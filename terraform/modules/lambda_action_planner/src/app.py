"""
Remediation Agent — Step 3 of the multi-agent remediation pipeline.

Receives the full Step Functions state:
  { s3_bucket, s3_key, incident_id, service, env, triage: {...}, diagnosis: {...} }

Returns:
  { action, params, target, reasoning, alternatives }

Results stored under $.remediation in the Step Functions state object.
"""
import base64
import json
import logging
import os
from datetime import datetime, timezone

import boto3
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import yaml

# ── Logger ────────────────────────────────────────────────────────────────────
COMPONENT = "action_planner"

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


# ── HTTP session ──────────────────────────────────────────────────────────────
def _make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

_SESSION = _make_session()

# ── AWS clients ───────────────────────────────────────────────────────────────
s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))
secrets = boto3.client("secretsmanager")

# ── Config ────────────────────────────────────────────────────────────────────
MODEL_PROVIDER          = os.environ.get("MODEL_PROVIDER", "bedrock")
BEDROCK_MODEL_ID        = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
OPENAI_SECRET_ARN       = os.environ.get("OPENAI_SECRET_ARN", "")
GITHUB_API              = "https://api.github.com"
GITHUB_OWNER            = os.environ.get("GITHUB_OWNER", "")
GITHUB_REPO             = os.environ.get("GITHUB_REPO", "")
GITHUB_TOKEN_SECRET_ARN = os.environ.get("GITHUB_APP_TOKEN_SECRET_ARN", "")
ALLOWED_ACTIONS_PATH    = os.environ.get("ALLOWED_ACTIONS_PATH", "gitops/policies/allowed-actions.yaml")

# ── GitHub token cache ────────────────────────────────────────────────────────
import time
_token_cache: dict = {"value": None, "expires_at": 0.0}


def _get_github_token() -> str:
    now = time.time()
    if _token_cache["value"] and now < _token_cache["expires_at"]:
        return _token_cache["value"]
    sec = json.loads(secrets.get_secret_value(SecretId=GITHUB_TOKEN_SECRET_ARN)["SecretString"])
    _token_cache.update({"value": sec["token"], "expires_at": now + 300})
    return sec["token"]


def _fetch_allowed_actions() -> dict:
    token = _get_github_token()
    r = _SESSION.get(
        f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{ALLOWED_ACTIONS_PATH}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        params={"ref": "main"},
        timeout=15,
    )
    r.raise_for_status()
    content = base64.b64decode(r.json()["content"]).decode("utf-8")
    return yaml.safe_load(content) or {}


def _call_llm(prompt: str) -> str:
    if MODEL_PROVIDER == "openai":
        sec = json.loads(secrets.get_secret_value(SecretId=OPENAI_SECRET_ARN)["SecretString"])
        r = _SESSION.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {sec['api_key']}", "Content-Type": "application/json"},
            json={"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": prompt}], "temperature": 0.2},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    else:
        resp = bedrock.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 800,
                "temperature": 0.2,
                "messages": [{"role": "user", "content": prompt}],
            }),
        )
        data = json.loads(resp["body"].read())
        return data["content"][0]["text"] if isinstance(data.get("content"), list) else ""


_REMEDIATION_SCHEMA = """{
  "action":       "<chosen action from allowed_actions>",
  "params":       {},
  "target":       {"service": "<string>", "env": "<string>"},
  "reasoning":    "<step-by-step reasoning for this choice>",
  "alternatives": ["<other action considered>"]
}"""


def handler(event, context):
    _init_log_context(context, incident_id=event.get("incident_id"))
    s3_bucket   = event["s3_bucket"]
    s3_key      = event["s3_key"]
    incident_id = event.get("incident_id", "unknown")
    triage      = event.get("triage", {})
    diagnosis   = event.get("diagnosis", {})

    bundle = json.loads(s3.get_object(Bucket=s3_bucket, Key=s3_key)["Body"].read())
    _log("info", "remediation_started", incident_id=incident_id,
         root_cause=diagnosis.get("root_cause"), confidence=diagnosis.get("diagnosis_confidence"))

    try:
        allowed = _fetch_allowed_actions()
    except Exception as exc:
        _log("warning", "fetch_allowed_actions_failed", error=str(exc))
        allowed = {}

    allowed_actions = [a["action"] for a in allowed.get("allowed_actions", [])]
    if not allowed_actions:
        allowed_actions = ["scale_replicas", "rollback_image", "restart_rollout", "tune_resources"]

    prompt = f"""You are an SRE remediation specialist. Propose the best fix for this incident.

Triage:
{json.dumps(triage, indent=2)}

Diagnosis:
{json.dumps(diagnosis, indent=2)}

Allowed actions: {allowed_actions}

Incident context (JSON):
{json.dumps(bundle)[:3000]}

Rules:
- Only choose from the allowed_actions list
- Prefer the least disruptive action that addresses the root cause
- Explain your reasoning step by step

Respond with valid JSON only matching this schema:
{_REMEDIATION_SCHEMA}
"""
    try:
        text   = _call_llm(prompt)
        result = json.loads(text)
        if result.get("action") not in allowed_actions:
            raise ValueError(f"Proposed action '{result.get('action')}' not in allowed list")
        for field in ("action", "params", "target", "reasoning"):
            if field not in result:
                raise ValueError(f"Missing field: {field}")
    except Exception as exc:
        _log("warning", "remediation_llm_failed_using_heuristic", error=str(exc))
        result = {
            "action":       "restart_rollout",
            "params":       {},
            "target":       {"service": bundle.get("service", "unknown"), "env": bundle.get("env", "staging")},
            "reasoning":    "Heuristic fallback: restart_rollout is the safest default action.",
            "alternatives": [],
        }

    _log("info", "remediation_complete", incident_id=incident_id, action=result["action"])
    return result
