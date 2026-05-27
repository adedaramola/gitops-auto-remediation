"""
Diagnosis Agent — Step 2 of the multi-agent remediation pipeline.

Receives the full Step Functions state:
  { s3_bucket, s3_key, incident_id, service, env, triage: {...}, ... }

Returns:
  { root_cause, contributing_factors, affected_components, diagnosis_confidence }

Results stored under $.diagnosis in the Step Functions state object.
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
COMPONENT = "root_cause_agent"

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

_DIAGNOSIS_SCHEMA = """{
  "root_cause":            "<concise root cause statement>",
  "contributing_factors":  ["<factor 1>", "<factor 2>"],
  "affected_components":   ["<component 1>", "<component 2>"],
  "diagnosis_confidence":  <0-100>
}"""


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
                "max_tokens": 700,
                "temperature": 0.2,
                "messages": [{"role": "user", "content": prompt}],
            }),
        )
        data = json.loads(resp["body"].read())
        return data["content"][0]["text"] if isinstance(data.get("content"), list) else ""


def _heuristic_diagnosis(bundle: dict, triage: dict) -> dict:
    """Fallback diagnosis when LLM is unavailable."""
    incident_type = triage.get("incident_type", "Unknown")
    causes = {
        "OOMKilled":      "Container exceeded memory limits",
        "HighErrorRate":  "Application returning 5xx errors — possible bug or downstream dependency failure",
        "PodCrashLoop":   "Pod repeatedly crashing — check application logs and resource limits",
        "NetworkLatency": "Elevated network latency — possible network congestion or DNS issues",
        "CPUThrottle":    "Container CPU being throttled — resource limits too restrictive",
    }
    return {
        "root_cause":           causes.get(incident_type, "Unknown root cause — insufficient telemetry"),
        "contributing_factors": [f"incident_type={incident_type}"],
        "affected_components":  [bundle.get("service", "unknown")],
        "diagnosis_confidence": 30,
    }


def handler(event, context):
    _init_log_context(context, incident_id=event.get("incident_id"))
    s3_bucket   = event["s3_bucket"]
    s3_key      = event["s3_key"]
    incident_id = event.get("incident_id", "unknown")
    triage      = event.get("triage", {})

    bundle = json.loads(s3.get_object(Bucket=s3_bucket, Key=s3_key)["Body"].read())
    _log("info", "diagnosis_started", incident_id=incident_id,
         incident_type=triage.get("incident_type"), severity=triage.get("severity_class"))

    prompt = f"""You are an SRE diagnosis specialist. Perform root cause analysis on this incident.

Triage classification:
{json.dumps(triage, indent=2)}

Incident bundle (JSON):
{json.dumps(bundle)[:4000]}

Focus on:
1. What is the most likely root cause?
2. What contributing factors are present in the telemetry?
3. Which components are affected?
4. How confident are you in this diagnosis? (0=guessing, 100=certain)

Respond with valid JSON only matching this schema:
{_DIAGNOSIS_SCHEMA}
"""
    try:
        text   = _call_llm(prompt)
        result = json.loads(text)
        for field in ("root_cause", "contributing_factors", "affected_components", "diagnosis_confidence"):
            if field not in result:
                raise ValueError(f"Missing field: {field}")
        result["diagnosis_confidence"] = max(0, min(100, int(result["diagnosis_confidence"])))
    except Exception as exc:
        _log("warning", "diagnosis_llm_failed_using_heuristic", error=str(exc))
        result = _heuristic_diagnosis(bundle, triage)

    _log("info", "diagnosis_complete", incident_id=incident_id,
         root_cause=result["root_cause"], confidence=result["diagnosis_confidence"])

    return result
