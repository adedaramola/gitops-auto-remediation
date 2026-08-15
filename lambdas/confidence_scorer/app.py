"""
Risk Agent — Step 4 of the multi-agent remediation pipeline.

Receives the full Step Functions state:
  { s3_bucket, s3_key, incident_id, triage: {...}, diagnosis: {...}, remediation: {...} }

Computes a confidence score and routes the pipeline:
  - confidence >= 80  → recommendation: "auto_apply"   (Phase 3)
  - confidence 40–79  → recommendation: "open_pr"      (open PR + Slack notification)
  - confidence < 40   → recommendation: "escalate"     (page on-call, no auto-action)

Returns:
  { confidence_score, risk_level, risk_factors, recommendation }

Results stored under $.risk in the Step Functions state object.
"""
import json
import logging
import os
from datetime import datetime, timezone

import boto3

# ── Logger ────────────────────────────────────────────────────────────────────
COMPONENT = "confidence_scorer"

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


# ── AWS clients ───────────────────────────────────────────────────────────────
s3 = boto3.client("s3")
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

# ── Scoring tables ────────────────────────────────────────────────────────────
_SEVERITY_PENALTY = {"critical": -25, "high": -15, "medium": -5, "low": 0}
_BLAST_PENALTY    = {"broad": -20, "contained": -10, "isolated": 0}
_ACTION_PENALTY   = {"rollback_image": -10, "scale_replicas": 0, "restart_rollout": -5, "tune_resources": -5}

# Actions that carry higher inherent risk regardless of confidence
_HIGH_RISK_ACTIONS = {"rollback_image", "tune_resources"}


def _score(triage: dict, diagnosis: dict, remediation: dict) -> tuple[int, str, list]:
    """
    Calculates a confidence score (0–100) from upstream agent results.
    Returns (score, risk_level, risk_factors).
    """
    factors = []
    # Base: diagnosis confidence (0–100)
    base = int(diagnosis.get("diagnosis_confidence", 50))

    # Penalty: incident severity
    severity   = triage.get("severity_class", "medium").lower()
    sev_pen    = _SEVERITY_PENALTY.get(severity, -5)
    if sev_pen < 0:
        factors.append(f"severity={severity} ({sev_pen}pts)")

    # Penalty: blast radius
    blast     = triage.get("blast_radius", "contained").lower()
    blast_pen = _BLAST_PENALTY.get(blast, -10)
    if blast_pen < 0:
        factors.append(f"blast_radius={blast} ({blast_pen}pts)")

    # Penalty: action inherent risk
    action    = remediation.get("action", "restart_rollout")
    act_pen   = _ACTION_PENALTY.get(action, -5)
    if act_pen < 0:
        factors.append(f"action={action} ({act_pen}pts)")

    # Penalty: low diagnosis confidence
    if base < 50:
        factors.append(f"low_diagnosis_confidence={base}")

    score = max(0, min(100, base + sev_pen + blast_pen + act_pen))

    # Risk level
    if score >= 75:
        risk_level = "low"
    elif score >= 45:
        risk_level = "medium"
    else:
        risk_level = "high"

    # Force high risk for certain actions regardless of score
    if action in _HIGH_RISK_ACTIONS and severity == "critical":
        risk_level = "high"
        if "forced_high_risk_action_on_critical" not in factors:
            factors.append(f"forced_high_risk: {action} on critical incident")

    return score, risk_level, factors


def _recommend(confidence_score: int, risk_level: str) -> str:
    if confidence_score >= 80 and risk_level == "low":
        return "auto_apply"
    if confidence_score >= 40:
        return "open_pr"
    return "escalate"


def handler(event, context):
    _init_log_context(context, incident_id=event.get("incident_id"))
    incident_id  = event.get("incident_id", "unknown")
    triage       = event.get("triage", {})
    diagnosis    = event.get("diagnosis", {})
    remediation  = event.get("remediation", {})

    _log("info", "risk_assessment_started", incident_id=incident_id,
         action=remediation.get("action"), diagnosis_confidence=diagnosis.get("diagnosis_confidence"))

    confidence_score, risk_level, risk_factors = _score(triage, diagnosis, remediation)
    if remediation.get("action") == "no_action":
        # Step Functions' escalate branch performs no GitHub mutation. This is
        # the safe terminal route for false positives and inconclusive signals.
        recommendation = "escalate"
        risk_factors.append("no_action_requested")
    else:
        recommendation = _recommend(confidence_score, risk_level)

    _put_metric("ConfidenceScore", value=float(confidence_score), unit="None")
    _put_metric("RoutingDecision", Decision=recommendation)

    result = {
        "confidence_score": confidence_score,
        "risk_level":       risk_level,
        "risk_factors":     risk_factors,
        "recommendation":   recommendation,
    }

    _log("info", "risk_assessment_complete", incident_id=incident_id,
         confidence_score=confidence_score, risk_level=risk_level, recommendation=recommendation)

    return result
