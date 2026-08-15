import base64
import json
import logging
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

import boto3
from botocore.credentials import Credentials
import botocore.session
from botocore.signers import RequestSigner
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── Structured JSON logger ────────────────────────────────────────────────────
COMPONENT = "outcome_validator"

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
def _make_session(total_retries: int = 3) -> requests.Session:
    s = requests.Session()
    retry = Retry(total=total_retries, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s

_SESSION = _make_session()
_NO_RETRY_SESSION = _make_session(total_retries=0)

# ── AWS clients ───────────────────────────────────────────────────────────────
events = boto3.client("events")
eks = boto3.client("eks")
secrets = boto3.client("secretsmanager")
dynamodb = boto3.client("dynamodb")
cw = boto3.client("cloudwatch")


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
import time as _time  # noqa: E402

GITHUB_API = "https://api.github.com"
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
GITHUB_TOKEN_SECRET_ARN = os.environ.get("GITHUB_APP_TOKEN_SECRET_ARN", "")
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "")
PROM_URL = os.environ.get("PROMETHEUS_QUERY_URL", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
AUTO_REVERT_ON_FAIL = os.environ.get("AUTO_REVERT_ON_FAIL", "true").lower() == "true"
AUDIT_TABLE_NAME = os.environ.get("AUDIT_TABLE_NAME", "")


def _audit_update(incident_id: str, outcome: str, detail: dict) -> None:
    """Update the audit record with the final validation outcome."""
    if not AUDIT_TABLE_NAME:
        return
    try:
        dynamodb.put_item(
            TableName=AUDIT_TABLE_NAME,
            Item={
                "incident_id": {"S": incident_id},
                "event_time":  {"N": str(int(_time.time()))},
                "ttl":         {"N": str(int(_time.time()) + 90 * 86400)},
                "stage":       {"S": "outcome_validated"},
                "outcome":     {"S": outcome},
                **{k: {"S": str(v)} for k, v in detail.items()},
            },
        )
    except Exception as exc:  # noqa: BLE001
        _log("warning", "audit_update_failed", error=str(exc))


def _get_secret_json(arn: str) -> dict:
    sec = secrets.get_secret_value(SecretId=arn)
    payload = sec.get("SecretString") or "{}"
    return json.loads(payload)


def _gh_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gh(method, path, token, **kwargs):
    url = f"{GITHUB_API}{path}"
    r = _SESSION.request(method, url, headers=_gh_headers(token), timeout=30, **kwargs)
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub API error {r.status_code}: {r.text}")
    return r.json() if r.text else {}


def _prom_query(q: str):
    if not PROM_URL:
        return {"skipped": True, "reason": "PROMETHEUS_QUERY_URL not set"}
    try:
        proxy_target = _prometheus_proxy_target(PROM_URL)
        if proxy_target:
            r = _prom_query_via_k8s_proxy(q, proxy_target)
        else:
            url = f"{PROM_URL.rstrip('/')}/api/v1/query"
            r = _SESSION.get(url, params={"query": q}, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as exc:
        return {"error": str(exc)}


def _prometheus_proxy_target(url: str):
    parsed = urlparse(url)
    host = parsed.hostname or ""
    parts = host.split(".")
    if len(parts) >= 3 and parts[2] == "svc":
        return {
            "scheme": parsed.scheme or "http",
            "service": parts[0],
            "namespace": parts[1],
            "port": parsed.port or (443 if parsed.scheme == "https" else 80),
        }
    return None


def _request_signer_credentials(session):
    creds = session.get_credentials()
    if hasattr(creds, "get_frozen_credentials"):
        creds = creds.get_frozen_credentials()
    return Credentials(
        creds.access_key,
        creds.secret_key,
        creds.token,
        getattr(creds, "method", None),
    )


def _eks_token(cluster_name: str) -> str:
    session = botocore.session.get_session()
    sts_model = session.get_service_model("sts")
    signer = RequestSigner(
        service_id=sts_model.service_id,
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        signing_name=sts_model.signing_name,
        signature_version="v4",
        credentials=_request_signer_credentials(session),
        event_emitter=session.get_component("event_emitter"),
    )
    params = {
        "method": "GET",
        "url": f"https://sts.{os.environ.get('AWS_REGION', 'us-east-1')}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
        "body": {},
        "headers": {"x-k8s-aws-id": cluster_name},
        "context": {},
    }
    signed = signer.generate_presigned_url(params, expires_in=60, operation_name="")
    return "k8s-aws-v1." + base64.urlsafe_b64encode(signed.encode("utf-8")).decode("utf-8").rstrip("=")


def _k8s_api(cluster_name: str):
    desc = eks.describe_cluster(name=cluster_name)["cluster"]
    endpoint = desc["endpoint"]
    ca = base64.b64decode(desc["certificateAuthority"]["data"])
    return endpoint, ca


def _prom_query_via_k8s_proxy(q: str, target: dict):
    cluster_name = os.environ.get("CLUSTER_NAME", "")
    if not cluster_name:
        raise requests.RequestException("CLUSTER_NAME is required for in-cluster Prometheus proxying")
    endpoint, ca = _k8s_api(cluster_name)
    ca_path = f"/tmp/{COMPONENT}-prom-ca.crt"  # nosec B108 — /tmp is the writable Lambda path
    with open(ca_path, "wb") as f:
        f.write(ca)
    token = _eks_token(cluster_name)
    proxy_path = (
        f"/api/v1/namespaces/{target['namespace']}/services/"
        f"{target['scheme']}:{target['service']}:{target['port']}/proxy/api/v1/query"
    )
    return _NO_RETRY_SESSION.get(
        endpoint.rstrip("/") + proxy_path,
        headers={"Authorization": f"Bearer {token}"},
        params={"query": q},
        timeout=4,
        verify=ca_path,
    )


def _emit(detail_type, detail):
    if not EVENT_BUS_NAME:
        return
    events.put_events(Entries=[{
        "EventBusName": EVENT_BUS_NAME,
        "Source": "gitops.sentinel",
        "DetailType": detail_type,
        "Detail": json.dumps(detail),
    }])


def _slack(msg: str):
    if not SLACK_WEBHOOK_URL:
        return
    try:
        _SESSION.post(SLACK_WEBHOOK_URL, json={"text": msg}, timeout=10).raise_for_status()
    except requests.RequestException as exc:
        _log("warning", "slack_notify_failed", error=str(exc))


def _extract_incident_id(detail: dict):
    return detail.get("incident_id") or detail.get("inc") or "unknown"


def _find_ai_pr_for_incident(token: str, incident_id: str):
    q = f'repo:{GITHUB_OWNER}/{GITHUB_REPO} "{incident_id}" in:title type:pr'
    res = _gh("GET", "/search/issues", token, params={"q": q})
    items = [
        item for item in res.get("items", [])
        if not str(item.get("title", "")).lower().startswith("revert remediation")
    ]
    if not items:
        return None
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return items[0]


def _get_pr_files(token: str, pr_number: int):
    files = []
    page = 1
    while True:
        res = _SESSION.get(
            f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/pulls/{pr_number}/files",
            headers=_gh_headers(token),
            params={"per_page": 100, "page": page},
            timeout=30,
        )
        res.raise_for_status()
        batch = res.json()
        if not batch:
            break
        files.extend(batch)
        page += 1
    return files


def _get_ref_sha(token: str, ref: str):
    data = _gh("GET", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/ref/{ref}", token)
    return data["object"]["sha"]


def _create_branch(token: str, branch: str, base_sha: str):
    return _gh("POST", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/refs", token, json={
        "ref": f"refs/heads/{branch}",
        "sha": base_sha,
    })


def _get_file(token: str, path: str, ref: str):
    return _gh("GET", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}", token, params={"ref": ref})


def _put_file(token: str, path: str, message: str, content_bytes: bytes, sha: str, branch: str):
    return _gh("PUT", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}", token, json={
        "message": message,
        "content": base64.b64encode(content_bytes).decode("utf-8"),
        "sha": sha,
        "branch": branch,
    })


def _open_pr(token: str, title: str, body: str, head: str, base: str):
    return _gh("POST", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/pulls", token, json={
        "title": title,
        "body": body,
        "head": head,
        "base": base,
    })


def _find_open_pr_for_branch(token: str, branch: str):
    prs = _gh(
        "GET",
        f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/pulls",
        token,
        params={"head": f"{GITHUB_OWNER}:{branch}", "state": "open"},
    )
    return prs[0] if prs else None


def _auto_revert(token: str, incident_id: str):
    pr_item = _find_ai_pr_for_incident(token, incident_id)
    if not pr_item:
        return {"skipped": True, "reason": "no_pr_found"}

    pr_number = pr_item["number"]
    pr = _gh("GET", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/pulls/{pr_number}", token)
    base_branch = pr["base"]["ref"]
    # GitHub's base.sha records the repository state before the remediation
    # merge. Reading current main here would copy the remediated content and
    # produce a no-op revert.
    original_base_ref = pr.get("base", {}).get("sha") or base_branch
    base_sha = _get_ref_sha(token, f"heads/{base_branch}")

    branch = f"ai/revert-{incident_id}"
    try:
        _create_branch(token, branch, base_sha)
    except RuntimeError:
        pass  # branch may already exist

    files = _get_pr_files(token, pr_number)
    changed_paths = [f["filename"] for f in files if f.get("status") in ("modified", "added", "removed")]

    restored = []
    for path in changed_paths:
        try:
            base_obj = _get_file(token, path, original_base_ref)
            base_content = base64.b64decode(base_obj["content"])
            try:
                cur_obj = _get_file(token, path, branch)
                cur_sha = cur_obj["sha"]
            except RuntimeError:
                cur_sha = None

            if cur_sha:
                _put_file(token, path, f"revert {incident_id}: restore {path}",
                          base_content, cur_sha, branch)
            else:
                _gh("PUT", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}", token, json={
                    "message": f"revert {incident_id}: restore {path}",
                    "content": base64.b64encode(base_content).decode("utf-8"),
                    "branch": branch,
                })
            restored.append(path)
        except Exception as exc:
            _log("warning", "revert_file_failed", path=path, error=str(exc))
            continue

    title = f"Revert remediation for {incident_id}"
    body = (
        f"Outcome Validator detected remediation failure for `{incident_id}`.\n\n"
        f"This PR restores files changed by remediation PR #{pr_number} "
        f"back to `{base_branch}` state.\n\nRestored paths:\n- "
        + "\n- ".join(restored)
    )

    revert_pr = _find_open_pr_for_branch(token, branch)
    if not revert_pr:
        revert_pr = _open_pr(token, title, body, head=branch, base=base_branch)
    _log("info", "revert_pr_opened", incident_id=incident_id,
         pr_number=revert_pr.get("number"), pr_url=revert_pr.get("html_url"))
    return {
        "revert_pr_url": revert_pr.get("html_url"),
        "revert_pr_number": revert_pr.get("number"),
        "restored": restored,
    }


def handler(event, context):
    _init_log_context(context)
    detail = event.get("detail", {}) if isinstance(event, dict) else {}
    incident_id = _extract_incident_id(detail)
    service = detail.get("service", "unknown")
    _REQUEST_CONTEXT["incident_id"] = incident_id
    _REQUEST_CONTEXT["service"] = service
    _log("info", "validator_started", incident_id=incident_id, service=service)

    err = _prom_query(f'sum(rate(http_requests_total{{service="{service}",status=~"5.."}}[5m]))')
    recovered = False
    try:
        res = (err.get("data") or {}).get("result") or []
        if res and "value" in res[0]:
            val = float(res[0]["value"][1])
            recovered = val < 0.2
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        _log("warning", "recovery_check_failed", error=str(exc))
        recovered = False

    status = "OutcomeValidated" if recovered else "OutcomeFailed"
    _log("info", "verification_result", incident_id=incident_id, status=status, recovered=recovered)
    _put_metric("OutcomeValidated" if recovered else "OutcomeFailed", Service=service)

    revert_result = None
    if (not recovered) and AUTO_REVERT_ON_FAIL and GITHUB_OWNER and GITHUB_REPO and GITHUB_TOKEN_SECRET_ARN:
        try:
            token = _get_secret_json(GITHUB_TOKEN_SECRET_ARN)["token"]
            revert_result = _auto_revert(token, incident_id)
        except Exception as exc:
            _log("error", "auto_revert_failed", incident_id=incident_id, error=str(exc))
            revert_result = {"error": str(exc)}

    payload = {
        "incident_id": incident_id,
        "service": service,
        "recovered": recovered,
        "prometheus": err,
        "revert": revert_result,
    }
    _emit(status, payload)

    msg = f"[GitOps Sentinel] {status} incident={incident_id} service={service} recovered={recovered}"
    if revert_result and revert_result.get("revert_pr_url"):
        msg += f" | Revert PR: {revert_result['revert_pr_url']}"
        _put_metric("RevertPROpened", Service=service)
    _slack(msg)

    _audit_update(incident_id, status, {
        "service":    service,
        "recovered":  str(recovered),
        "revert_url": (revert_result or {}).get("revert_pr_url", ""),
    })

    return {"statusCode": 200, "body": json.dumps(payload)}
