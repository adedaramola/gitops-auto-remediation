"""
Triage Agent — Step 1 of the multi-agent remediation pipeline.

Receives: { s3_bucket, s3_key, incident_id, service, env, ... }
Returns:  { severity_class, incident_type, blast_radius, priority, key_signals }

Results are stored under $.triage in the Step Functions state object.
"""
import json
import logging
import os
from datetime import datetime, timezone

import boto3
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── Logger ────────────────────────────────────────────────────────────────────
COMPONENT = "classifier_agent"

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
MODEL_PROVIDER    = os.environ.get("MODEL_PROVIDER", "bedrock")
BEDROCK_MODEL_ID  = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
OPENAI_SECRET_ARN = os.environ.get("OPENAI_SECRET_ARN", "")

_TRIAGE_SCHEMA = """{
  "severity_class": "<critical|high|medium|low>",
  "incident_type":  "<OOMKilled|HighErrorRate|PodCrashLoop|NetworkLatency|DiskPressure|CPUThrottle|Unknown>",
  "blast_radius":   "<isolated|contained|broad>",
  "priority":       <1-5>,
  "key_signals":    ["<most important data points from the bundle, max 5>"]
}"""


def _call_llm(prompt: str) -> str:
    if MODEL_PROVIDER == "openai":
        sec = json.loads(secrets.get_secret_value(SecretId=OPENAI_SECRET_ARN)["SecretString"])
        r = _SESSION.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {sec['api_key']}", "Content-Type": "application/json"},
            json={"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": prompt}], "temperature": 0.1},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    else:
        resp = bedrock.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 500,
                "temperature": 0.1,
                "messages": [{"role": "user", "content": prompt}],
            }),
        )
        data = json.loads(resp["body"].read())
        return data["content"][0]["text"] if isinstance(data.get("content"), list) else ""


def _heuristic_triage(bundle: dict) -> dict:
    """Fallback triage when LLM is unavailable."""
    severity = bundle.get("severity", "unknown").lower()
    severity_map = {"critical": "critical", "warning": "high", "info": "medium"}
    return {
        "severity_class": severity_map.get(severity, "medium"),
        "incident_type":  "Unknown",
        "blast_radius":   "contained",
        "priority":       2 if severity == "critical" else 3,
        "key_signals":    [f"alertname={bundle.get('labels', {}).get('alertname', 'unknown')}"],
    }


def handler(event, context):
    _init_log_context(context, incident_id=event.get("incident_id"))
    s3_bucket   = event["s3_bucket"]
    s3_key      = event["s3_key"]
    incident_id = event.get("incident_id", "unknown")

    bundle = json.loads(s3.get_object(Bucket=s3_bucket, Key=s3_key)["Body"].read())
    _log("info", "triage_started", incident_id=incident_id)

    prompt = f"""You are an SRE triage specialist. Analyse this incident bundle and classify it.

Incident bundle (JSON):
{json.dumps(bundle)[:4000]}

Respond with valid JSON only matching this schema:
{_TRIAGE_SCHEMA}
"""
    try:
        text   = _call_llm(prompt)
        result = json.loads(text)
        # Validate required fields
        for field in ("severity_class", "incident_type", "blast_radius", "priority", "key_signals"):
            if field not in result:
                raise ValueError(f"Missing field: {field}")
    except Exception as exc:
        _log("warning", "triage_llm_failed_using_heuristic", error=str(exc))
        result = _heuristic_triage(bundle)

    _log("info", "triage_complete", incident_id=incident_id,
         severity_class=result["severity_class"], incident_type=result["incident_type"],
         blast_radius=result["blast_radius"], priority=result["priority"])

    return result
