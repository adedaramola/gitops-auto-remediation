import base64
import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import yaml

# ── Structured JSON logger ────────────────────────────────────────────────────
COMPONENT = "decision_engine"

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)
if not LOG.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(message)s"))
    LOG.addHandler(_h)
    LOG.propagate = False

_REQUEST_CONTEXT: dict = {}


def _init_log_context(lambda_context=None, **extra):
    """Seed per-invocation log fields. Call once at the top of each handler."""
    _REQUEST_CONTEXT.clear()
    if lambda_context is not None:
        rid = getattr(lambda_context, "aws_request_id", None)
        if rid:
            _REQUEST_CONTEXT["request_id"] = rid
    trace_header = os.environ.get("_X_AMZN_TRACE_ID", "")
    for part in trace_header.split(";"):
        if part.startswith("Root="):
            _REQUEST_CONTEXT["trace_id"] = part[5:]
            break
    for k, v in extra.items():
        if v is not None:
            _REQUEST_CONTEXT[k] = v


def _log(level: str, msg: str, **ctx):
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "level": level.upper(),
        "component": COMPONENT,
        "msg": msg,
        **_REQUEST_CONTEXT,
        **ctx,
    }
    LOG.log(getattr(logging, level.upper()), json.dumps(record, default=str))


# ── HTTP session with retries ─────────────────────────────────────────────────
def _make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

_SESSION = _make_session()

# ── AWS clients ───────────────────────────────────────────────────────────────
s3 = boto3.client("s3")
secrets = boto3.client("secretsmanager")
bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))
dynamodb = boto3.client("dynamodb")
cw = boto3.client("cloudwatch")
events = boto3.client("events")
ssm = boto3.client("ssm")


def _put_metric(name: str, value: float = 1.0, unit: str = "Count", **dims):
    try:
        cw.put_metric_data(
            Namespace="GitOpsSentinel",
            MetricData=[{
                "MetricName": name,
                "Value": value,
                "Unit": unit,
                "Dimensions": [{"Name": k, "Value": str(v)} for k, v in dims.items()],
            }],
        )
    except Exception:
        pass


# ── Config from environment ───────────────────────────────────────────────────
GITHUB_API = "https://api.github.com"
GITHUB_OWNER = os.environ["GITHUB_OWNER"]
GITHUB_REPO = os.environ["GITHUB_REPO"]
GITHUB_APP_TOKEN_SECRET_ARN = os.environ["GITHUB_APP_TOKEN_SECRET_ARN"]
MODEL_PROVIDER = os.environ.get("MODEL_PROVIDER", "bedrock")
ALLOWED_ACTIONS_PATH = os.environ.get("ALLOWED_ACTIONS_PATH", "gitops/policies/allowed-actions.yaml")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
OPENAI_SECRET_ARN = os.environ.get("OPENAI_SECRET_ARN", "")
AUDIT_TABLE_NAME = os.environ.get("AUDIT_TABLE_NAME", "")
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "")
AUTO_APPLY_ENABLED_PARAM = os.environ.get("AUTO_APPLY_ENABLED_PARAM", "")
AUTO_APPLY_MAX_PER_HOUR = int(os.environ.get("AUTO_APPLY_MAX_PER_HOUR", "3"))

# ── GitHub token cache (persists across warm Lambda invocations) ──────────────
_token_cache: dict = {"value": None, "expires_at": 0.0}


def _parse_llm_json(text: str) -> dict:
    """Extract the JSON object from an LLM reply, tolerating markdown fences or prose."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in LLM reply")
    return json.loads(text[start:end + 1])


def _audit_write(incident_id: str, record: dict) -> None:
    """Write a decision record to the audit log table. Fails silently."""
    if not AUDIT_TABLE_NAME:
        return
    try:
        dynamodb.put_item(
            TableName=AUDIT_TABLE_NAME,
            Item={
                "incident_id": {"S": incident_id},
                "event_time":  {"N": str(int(time.time()))},
                "ttl":         {"N": str(int(time.time()) + 90 * 86400)},
                **{k: {"S": str(v)} for k, v in record.items()},
            },
        )
    except Exception as exc:  # noqa: BLE001
        _log("warning", "audit_write_failed", error=str(exc))


def _get_secret_json(arn: str) -> dict:
    sec = secrets.get_secret_value(SecretId=arn)
    payload = sec.get("SecretString") or "{}"
    return json.loads(payload)


def _emit(detail_type: str, detail: dict) -> None:
    if not EVENT_BUS_NAME:
        return
    events.put_events(Entries=[{
        "EventBusName": EVENT_BUS_NAME,
        "Source": "gitops.sentinel",
        "DetailType": detail_type,
        "Detail": json.dumps(detail),
    }])


def _get_github_token() -> str:
    now = time.time()
    if _token_cache["value"] and now < _token_cache["expires_at"]:
        return _token_cache["value"]
    token = _get_secret_json(GITHUB_APP_TOKEN_SECRET_ARN)["token"]
    _token_cache["value"] = token
    _token_cache["expires_at"] = now + 300  # cache for 5 minutes
    _log("info", "github_token_refreshed")
    return token


def _github_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gh(method, path, token, **kwargs):
    url = f"{GITHUB_API}{path}"
    r = _SESSION.request(method, url, headers=_github_headers(token), timeout=30, **kwargs)
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub API error {r.status_code}: {r.text}")
    return r.json() if r.text else {}


def _get_ref_sha(owner, repo, ref, token):
    data = _gh("GET", f"/repos/{owner}/{repo}/git/ref/{ref}", token)
    return data["object"]["sha"]


def _create_branch(owner, repo, branch, base_sha, token):
    return _gh("POST", f"/repos/{owner}/{repo}/git/refs", token, json={
        "ref": f"refs/heads/{branch}",
        "sha": base_sha,
    })


def _get_file(owner, repo, path, ref, token):
    return _gh("GET", f"/repos/{owner}/{repo}/contents/{path}", token, params={"ref": ref})


def _put_file(owner, repo, path, message, content_bytes, sha, branch, token):
    return _gh("PUT", f"/repos/{owner}/{repo}/contents/{path}", token, json={
        "message": message,
        "content": base64.b64encode(content_bytes).decode("utf-8"),
        "sha": sha,
        "branch": branch,
    })


def _find_existing_pr(owner, repo, branch, token):
    """Returns an open PR for the branch if one already exists, else None."""
    results = _gh("GET", f"/repos/{owner}/{repo}/pulls", token,
                  params={"head": f"{owner}:{branch}", "state": "open"})
    return results[0] if results else None


def _open_pr(owner, repo, title, body, head, base, token):
    return _gh("POST", f"/repos/{owner}/{repo}/pulls", token, json={
        "title": title,
        "body": body,
        "head": head,
        "base": base,
    })


def _merge_pr(owner, repo, pr_number, token, commit_message=""):
    payload = {"merge_method": "squash"}
    if commit_message:
        payload["commit_message"] = commit_message
    return _gh("PUT", f"/repos/{owner}/{repo}/pulls/{pr_number}/merge", token, json=payload)


# Kill-switch cache: flipping the SSM parameter takes effect within 30s
# on warm Lambdas without an SSM call per invocation.
_kill_switch_cache: dict = {"value": None, "expires_at": 0.0}


def _auto_apply_enabled() -> bool:
    """SSM kill switch for the auto-merge path. Fails closed: if the parameter
    exists but cannot be read, or reads anything other than 'true', auto-apply
    is disabled and PRs are left open for human review. An unset
    AUTO_APPLY_ENABLED_PARAM (param not provisioned) leaves the gate open."""
    if not AUTO_APPLY_ENABLED_PARAM:
        return True
    now = time.time()
    if _kill_switch_cache["value"] is not None and now < _kill_switch_cache["expires_at"]:
        return _kill_switch_cache["value"]
    try:
        resp = ssm.get_parameter(Name=AUTO_APPLY_ENABLED_PARAM)
        enabled = resp["Parameter"]["Value"].strip().lower() == "true"
    except Exception as exc:  # noqa: BLE001
        _log("warning", "kill_switch_read_failed", error=str(exc))
        enabled = False
    _kill_switch_cache["value"] = enabled
    _kill_switch_cache["expires_at"] = now + 30
    return enabled


def _auto_apply_rate_ok() -> bool:
    """Hourly rate limit via an atomic counter in the audit table
    (incident_id='rate#auto_apply', event_time=<hour bucket>). Counts merge
    attempts, so a failed merge still consumes budget — conservative by
    design. Fails closed if the counter cannot be updated."""
    if not AUDIT_TABLE_NAME:
        return True
    now = int(time.time())
    try:
        resp = dynamodb.update_item(
            TableName=AUDIT_TABLE_NAME,
            Key={
                "incident_id": {"S": "rate#auto_apply"},
                "event_time":  {"N": str(now // 3600)},
            },
            UpdateExpression="ADD applied :one SET #t = if_not_exists(#t, :expiry)",
            ExpressionAttributeNames={"#t": "ttl"},
            ExpressionAttributeValues={
                ":one":    {"N": "1"},
                ":expiry": {"N": str(now + 7 * 86400)},
            },
            ReturnValues="UPDATED_NEW",
        )
        count = int(resp["Attributes"]["applied"]["N"])
    except Exception as exc:  # noqa: BLE001
        _log("warning", "rate_limit_check_failed", error=str(exc))
        return False
    return count <= AUTO_APPLY_MAX_PER_HOUR


def _auto_apply(pr, incident_id, action, service, env, token) -> bool:
    """High-confidence path: merge the PR and emit ActionDispatched so the
    Outcome Validator is triggered natively via EventBridge. Returns True if
    merged; when a guardrail blocks or the merge fails (branch protection,
    pending checks) the PR is left open for human review instead."""
    if not _auto_apply_enabled():
        _log("warning", "auto_apply_blocked", reason="kill_switch",
             incident_id=incident_id, pr_number=pr.get("number"))
        _put_metric("AutoApplyBlocked", Reason="kill_switch")
        return False
    if not _auto_apply_rate_ok():
        _log("warning", "auto_apply_blocked", reason="rate_limited",
             incident_id=incident_id, pr_number=pr.get("number"))
        _put_metric("AutoApplyBlocked", Reason="rate_limited")
        return False
    try:
        # Marker tells the Notify Action Dispatched workflow to skip emission:
        # this path emits ActionDispatched itself, and a second event would
        # run the Outcome Validator twice.
        _merge_pr(GITHUB_OWNER, GITHUB_REPO, pr["number"], token,
                  commit_message=f"{incident_id}: auto-applied by decision engine")
    except RuntimeError as exc:
        _log("warning", "auto_merge_failed", incident_id=incident_id,
             pr_number=pr.get("number"), error=str(exc))
        _put_metric("AutoMergeFailures", Action=action)
        return False
    _log("info", "pr_auto_merged", incident_id=incident_id,
         pr_number=pr.get("number"), pr_url=pr.get("html_url"))
    _put_metric("PRsAutoApplied", Action=action)
    _emit("ActionDispatched", {
        "incident_id": incident_id,
        "service": service,
        "env": env,
        "action": action,
        "pr_number": pr.get("number"),
        "pr_url": pr.get("html_url"),
        "route": "auto_apply",
    })
    return True


def _fetch_allowed_actions(token, ref="main") -> dict:
    obj = _get_file(GITHUB_OWNER, GITHUB_REPO, ALLOWED_ACTIONS_PATH, ref, token)
    data = base64.b64decode(obj["content"]).decode("utf-8")
    return yaml.safe_load(data) or {}


def _choose_action_heuristic(bundle, allowed):
    """Safe-by-default heuristic. LLM can override within allowed actions."""
    actions = {a["action"]: a.get("constraints", {}) for a in allowed.get("allowed_actions", [])}
    prom = bundle.get("prometheus", {})
    err = prom.get("error_rate_5xx", {})
    if "result" in (err.get("data") or {}) and "rollback_image" in actions:
        return {
            "action": "rollback_image",
            "target": {"env": bundle.get("env", "staging")},
            "params": {"tag": "previous"},
        }
    if "scale_replicas" in actions:
        return {
            "action": "scale_replicas",
            "target": {"env": bundle.get("env", "staging")},
            "params": {"replicas": 3},
        }
    return {"action": "restart_rollout", "target": {"env": bundle.get("env", "staging")}, "params": {}}


def _llm_plan(bundle, allowed):
    """Returns a JSON dict: action, target, params, rationale, risk.
    Ensures the returned action is within the allowed list."""
    allowed_actions = [a["action"] for a in allowed.get("allowed_actions", [])]
    prompt = f"""
You are an SRE assistant operating under strict GitOps controls.
You must propose ONE remediation within allowed actions only.

Allowed actions: {allowed_actions}

Incident bundle (JSON):
{json.dumps(bundle)[:6000]}

Respond with valid JSON only:
{{
  "action": "<one of allowed actions>",
  "target": {{"service": "<string>", "env": "<string>"}},
  "params": {{}},
  "risk": "<low|medium|high>",
  "rationale": "<short>"
}}
"""
    try:
        if MODEL_PROVIDER == "openai":
            if not OPENAI_SECRET_ARN:
                raise ValueError("OPENAI_SECRET_ARN not set")
            api_key = _get_secret_json(OPENAI_SECRET_ARN)["api_key"]
            r = _SESSION.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "gpt-4.1-mini",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                },
                timeout=30,
            )
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
        else:
            model_id = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 700,
                "temperature": 0.2,
                "messages": [{"role": "user", "content": prompt}],
            })
            resp = bedrock.invoke_model(modelId=model_id, body=body)
            raw = resp["body"].read().decode("utf-8")
            data = json.loads(raw)
            text = ""
            if isinstance(data.get("content"), list) and data["content"]:
                text = data["content"][0].get("text", "")
            else:
                text = raw

        plan = _parse_llm_json(text)
        if plan.get("action") not in allowed_actions:
            raise ValueError(f"LLM returned disallowed action: {plan.get('action')}")
        _log("info", "llm_plan_selected", action=plan.get("action"), risk=plan.get("risk"))
        return plan

    except Exception as exc:
        _log("warning", "llm_plan_fallback", error=str(exc))
        p = _choose_action_heuristic(bundle, allowed)
        p.update({"risk": "low", "rationale": "Fallback heuristic plan."})
        p.setdefault("target", {}).setdefault("service", bundle.get("service", "unknown"))
        p.setdefault("target", {}).setdefault("env", bundle.get("env", "staging"))
        return p


def _patch_replicas_kustomize(kustomize_text: str, new_replicas: int) -> str:
    """Parse the kustomization YAML and update the /spec/replicas JSON Patch value."""
    doc = yaml.safe_load(kustomize_text)
    patched = False
    for patch_entry in doc.get("patches", []):
        patch_str = patch_entry.get("patch", "")
        if not patch_str:
            continue
        ops = yaml.safe_load(patch_str)
        if not isinstance(ops, list):
            continue
        for op in ops:
            if op.get("path") == "/spec/replicas":
                op["value"] = new_replicas
                patched = True
        if patched:
            patch_entry["patch"] = yaml.dump(ops, default_flow_style=False).rstrip()
            break
    if not patched:
        raise ValueError("Could not locate /spec/replicas patch operation.")
    return yaml.dump(doc, default_flow_style=False)


def _annotate_pod_template(doc: dict, key: str, value: str) -> None:
    """Set spec.template.metadata.annotations[key] = value in-place."""
    if not value:
        return
    (doc.setdefault("spec", {})
        .setdefault("template", {})
        .setdefault("metadata", {})
        .setdefault("annotations", {})
        [key]) = value


def _patch_image_deployment(deploy_yaml: str, new_tag: str, incident_id: str = "") -> str:
    """Parse the deployment YAML, replace the first container's image tag,
    and annotate the pod template with the incident id for log correlation."""
    doc = yaml.safe_load(deploy_yaml)
    try:
        containers = doc["spec"]["template"]["spec"]["containers"]
        if containers:
            base = containers[0]["image"].rsplit(":", 1)[0]
            containers[0]["image"] = f"{base}:{new_tag}"
    except (KeyError, IndexError, TypeError):
        pass  # no image found; return unchanged
    _annotate_pod_template(doc, "gitops.sentinel/incident-id", incident_id)
    return yaml.dump(doc, default_flow_style=False)


def handler(event, context):
    """Triggered by EventBridge on SignalBundled. Reads incident bundle
    from S3, proposes remediation via LLM, and opens a PR."""
    _init_log_context(context)
    detail = event.get("detail") or event
    bucket = detail["s3_bucket"]
    key = detail["s3_key"]
    # Step Functions passes the full pipeline state as detail; the confidence
    # scorer's recommendation selects the auto_apply route.
    route = (detail.get("risk") or {}).get("recommendation", "")

    bundle = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8"))
    incident_id = bundle["incident_id"]
    _REQUEST_CONTEXT["incident_id"] = incident_id
    _log("info", "agent_started", incident_id=incident_id)

    token = _get_github_token()
    allowed = _fetch_allowed_actions(token, ref="main")

    plan = _llm_plan(bundle, allowed)
    action = plan["action"]
    env = (plan.get("target") or {}).get("env") or bundle.get("env", "staging")
    service = (plan.get("target") or {}).get("service") or bundle.get("service", "unknown")

    branch = f"ai/{incident_id}-{action}"
    base_branch = "main"
    pr_title = f"{incident_id}: {action} for {service} ({env})"
    pr_body = f"""## Incident
- Incident ID: `{incident_id}`
- Bundle: s3://{bucket}/{key}

## Proposed remediation
- Action: `{action}`
- Target: `{service}` ({env})
- Params: `{json.dumps(plan.get('params', {}))}`
- Risk: `{plan.get('risk', 'low')}`

## Rationale
{plan.get('rationale', '')}

## Guardrails
- Git-only change (no direct cluster writes)
- CI + policy checks required before merge
- Admission control enforced by Gatekeeper (if installed)

## Rollback
Revert this PR.
"""

    # ── Idempotency: reuse existing PR if already open for this branch ────────
    existing_pr = _find_existing_pr(GITHUB_OWNER, GITHUB_REPO, branch, token)
    if existing_pr:
        _log("info", "pr_already_exists", incident_id=incident_id,
             pr_number=existing_pr.get("number"), pr_url=existing_pr.get("html_url"))
        auto_applied = False
        if route == "auto_apply":  # retry after a partial run: finish the merge
            auto_applied = _auto_apply(existing_pr, incident_id, action, service, env, token)
        return {"statusCode": 200, "body": json.dumps({
            "message": "PR auto-applied" if auto_applied else "PR already exists",
            "incident_id": incident_id,
            "action": action,
            "pr_number": existing_pr.get("number"),
            "pr_url": existing_pr.get("html_url"),
        })}

    base_sha = _get_ref_sha(GITHUB_OWNER, GITHUB_REPO, f"heads/{base_branch}", token)
    try:
        _create_branch(GITHUB_OWNER, GITHUB_REPO, branch, base_sha, token)
    except RuntimeError:
        pass  # branch already exists from a previous partial run

    changes = []

    if action == "scale_replicas":
        target_path = f"gitops/apps/{service}/overlays/{env}/kustomization.yaml"
        file_obj = _get_file(GITHUB_OWNER, GITHUB_REPO, target_path, base_branch, token)
        original = base64.b64decode(file_obj["content"]).decode("utf-8")
        replicas = int(plan.get("params", {}).get("replicas", 3))
        patched = _patch_replicas_kustomize(original, replicas).encode("utf-8")
        _put_file(GITHUB_OWNER, GITHUB_REPO, target_path,
                  f"{incident_id}: scale replicas", patched, file_obj["sha"], branch, token)
        changes.append(target_path)

    elif action == "rollback_image":
        tag = plan.get("params", {}).get("tag", "previous")
        deploy_path = f"gitops/apps/{service}/base/deployment.yaml"
        file_obj = _get_file(GITHUB_OWNER, GITHUB_REPO, deploy_path, base_branch, token)
        original = base64.b64decode(file_obj["content"]).decode("utf-8")
        patched = _patch_image_deployment(original, tag, incident_id).encode("utf-8")
        _put_file(GITHUB_OWNER, GITHUB_REPO, deploy_path,
                  f"{incident_id}: rollback image", patched, file_obj["sha"], branch, token)
        changes.append(deploy_path)

    elif action == "tune_resources":
        deploy_path = f"gitops/apps/{service}/base/deployment.yaml"
        file_obj = _get_file(GITHUB_OWNER, GITHUB_REPO, deploy_path, base_branch, token)
        original = base64.b64decode(file_obj["content"]).decode("utf-8")
        params = plan.get("params", {})
        mem_target = params.get("memory")
        cpu_target = params.get("cpu")
        doc = yaml.safe_load(original)
        for container in (doc.get("spec", {}).get("template", {})
                             .get("spec", {}).get("containers", [])):
            limits = container.setdefault("resources", {}).setdefault("limits", {})
            if mem_target:
                limits["memory"] = mem_target
            if cpu_target:
                limits["cpu"] = cpu_target
        _annotate_pod_template(doc, "gitops.sentinel/incident-id", incident_id)
        patched = yaml.dump(doc, default_flow_style=False).encode("utf-8")
        _put_file(GITHUB_OWNER, GITHUB_REPO, deploy_path,
                  f"{incident_id}: tune resources", patched, file_obj["sha"], branch, token)
        changes.append(deploy_path)

    elif action == "restart_rollout":
        deploy_path = f"gitops/apps/{service}/base/deployment.yaml"
        file_obj = _get_file(GITHUB_OWNER, GITHUB_REPO, deploy_path, base_branch, token)
        original = base64.b64decode(file_obj["content"]).decode("utf-8")
        stamp = str(int(time.time()))
        doc = yaml.safe_load(original)
        _annotate_pod_template(doc, "gitops.sentinel/restartedAt", stamp)
        _annotate_pod_template(doc, "gitops.sentinel/incident-id", incident_id)
        patched = yaml.dump(doc, default_flow_style=False).encode("utf-8")
        _put_file(GITHUB_OWNER, GITHUB_REPO, deploy_path,
                  f"{incident_id}: restart rollout", patched,
                  file_obj["sha"], branch, token)
        changes.append(deploy_path)

    pr = _open_pr(GITHUB_OWNER, GITHUB_REPO, pr_title, pr_body, head=branch, base=base_branch, token=token)
    _log("info", "pr_opened", incident_id=incident_id, action=action,
         pr_number=pr.get("number"), pr_url=pr.get("html_url"))
    _put_metric("PRsOpened", Action=action)

    auto_applied = False
    if route == "auto_apply":
        auto_applied = _auto_apply(pr, incident_id, action, service, env, token)

    _audit_write(incident_id, {
        "stage":       "action_dispatched",
        "action":      action,
        "service":     service,
        "env":         env,
        "confidence":  str(plan.get("risk", "unknown")),
        "rationale":   plan.get("rationale", ""),
        "pr_url":      pr.get("html_url", ""),
        "outcome":     "auto_applied" if auto_applied else "pending",
    })

    return {"statusCode": 200, "body": json.dumps({
        "message": "PR auto-applied" if auto_applied else "PR opened",
        "incident_id": incident_id,
        "action": action,
        "changed_files": changes,
        "pr_number": pr.get("number"),
        "pr_url": pr.get("html_url"),
    })}
