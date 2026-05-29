import base64
import hashlib
import hmac
import json
import logging
import os
import time
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
COMPONENT = "signal_collector"

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
s3 = boto3.client("s3")
events = boto3.client("events")
eks = boto3.client("eks")
ddb = boto3.client("dynamodb")
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
INCIDENT_BUCKET = os.environ["INCIDENT_BUCKET"]
EVENT_BUS_NAME = os.environ["EVENT_BUS_NAME"]
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
CLUSTER_NAME = os.environ.get("CLUSTER_NAME", "")
PROM_URL = os.environ.get("PROMETHEUS_QUERY_URL", "")
ENABLE_K8S = os.environ.get("ENABLE_K8S_READONLY", "true").lower() == "true"
INCIDENTS_TABLE_NAME = os.environ.get("INCIDENTS_TABLE_NAME", "")
DEDUP_TTL_SECONDS = int(os.environ.get("DEDUP_TTL_SECONDS", "1800"))
ENABLE_AMP = os.environ.get("ENABLE_AMP", "false").lower() == "true"
WEBHOOK_SECRET       = os.environ.get("WEBHOOK_SECRET", "")
# When True, emits SentinelPipelineTriggered instead of SignalBundled,
# routing the incident into the Step Functions multi-agent pipeline.
ENABLE_MULTI_AGENT   = os.environ.get("ENABLE_MULTI_AGENT", "false").lower() == "true"


def _now():
    return int(time.time())


def _emit(detail_type, detail):
    events.put_events(Entries=[{
        "EventBusName": EVENT_BUS_NAME,
        "Source": "gitops.sentinel",
        "DetailType": detail_type,
        "Detail": json.dumps(detail),
    }])


def _dedup_key(service: str, env: str, alertname: str) -> str:
    raw = f"{service}|{env}|{alertname}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _dedup_check_and_write(dedup_key: str):
    """Returns (is_new, existing_incident_id_or_none). Uses conditional put with TTL."""
    if not INCIDENTS_TABLE_NAME:
        return True, None
    now = _now()
    try:
        ddb.put_item(
            TableName=INCIDENTS_TABLE_NAME,
            Item={
                "dedup_key": {"S": dedup_key},
                "created_at": {"N": str(now)},
                "ttl": {"N": str(now + DEDUP_TTL_SECONDS)},
            },
            ConditionExpression="attribute_not_exists(dedup_key)",
        )
        return True, None
    except ddb.exceptions.ConditionalCheckFailedException:
        return False, None
    except Exception as exc:
        _log("warning", "dedup_write_failed", error=str(exc))
        return True, None  # fail open: process the incident


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
        region_name=AWS_REGION,
        signing_name=sts_model.signing_name,
        signature_version="v4",
        credentials=_request_signer_credentials(session),
        event_emitter=session.get_component("event_emitter"),
    )
    params = {
        "method": "GET",
        "url": f"https://sts.{AWS_REGION}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
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


def _k8s_get(endpoint: str, token: str, path: str, ca_path: str):
    url = endpoint.rstrip("/") + path
    r = _SESSION.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10, verify=ca_path)
    r.raise_for_status()
    return r.json()


def _prom_query_via_k8s_proxy(q: str, target: dict):
    if not CLUSTER_NAME:
        raise requests.RequestException("CLUSTER_NAME is required for in-cluster Prometheus proxying")
    endpoint, ca = _k8s_api(CLUSTER_NAME)
    ca_path = f"/tmp/{COMPONENT}-prom-ca.crt"  # nosec B108 — /tmp is the writable Lambda path
    with open(ca_path, "wb") as f:
        f.write(ca)
    token = _eks_token(CLUSTER_NAME)
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


def _extract_webhook_secret(headers: dict) -> str:
    """Return the secret from x-webhook-secret or Authorization: Bearer <token>."""
    if not headers:
        return ""
    if direct := headers.get("x-webhook-secret"):
        return direct
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:]
    return ""


def handler(event, context):
    _init_log_context(context)
    # ── Webhook secret validation ─────────────────────────────────────────────
    if WEBHOOK_SECRET:
        incoming = _extract_webhook_secret(event.get("headers") or {})
        if not hmac.compare_digest(incoming, WEBHOOK_SECRET):
            _log("warning", "webhook_auth_failed")
            return {"statusCode": 401, "body": json.dumps({"error": "unauthorized"})}

    incident_id = f"inc-{_now()}-{context.aws_request_id[:8]}"
    _REQUEST_CONTEXT["incident_id"] = incident_id
    _log("info", "incident_received", incident_id=incident_id)

    raw = event
    if "requestContext" in event and "body" in event:  # API Gateway HTTP API proxy
        body = event.get("body") or "{}"
        try:
            raw = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            raw = {"body_raw": body}

    labels = {}
    annotations = {}
    try:
        if isinstance(raw, dict) and raw.get("alerts"):
            labels = raw["alerts"][0].get("labels", {}) or {}
            annotations = raw["alerts"][0].get("annotations", {}) or {}
    except (KeyError, IndexError, TypeError) as exc:
        _log("warning", "alert_parse_failed", error=str(exc))

    alertname = labels.get("alertname", "unknown")
    service = labels.get("service", "unknown")
    namespace = labels.get("namespace", labels.get("env", "unknown"))
    env = labels.get("env", labels.get("environment", "unknown"))
    severity = labels.get("severity", "unknown")

    # ── Dedup / correlation guard ─────────────────────────────────────────────
    dk = _dedup_key(service, env, alertname)
    is_new, _ = _dedup_check_and_write(dk)
    if not is_new:
        _log("info", "dedup_suppressed", dedup_key=dk, service=service, alertname=alertname)
        _put_metric("IncidentsDeduplicated", Service=service)
        return {"statusCode": 202, "body": json.dumps({"message": "dedup_suppressed", "dedup_key": dk})}

    # ── Prometheus enrichment ─────────────────────────────────────────────────
    prom_snapshots = {
        "error_rate_5xx": _prom_query(
            f'sum(rate(http_requests_total{{service="{service}",status=~"5.."}}[5m]))'
        ),
        "cpu_usage": _prom_query(
            f'sum(rate(container_cpu_usage_seconds_total{{namespace="{namespace}",pod=~"{service}.*"}}[5m]))'
        ),
        "mem_working_set": _prom_query(
            f'sum(container_memory_working_set_bytes{{namespace="{namespace}",pod=~"{service}.*"}})'
        ),
    }

    # ── Kubernetes enrichment ─────────────────────────────────────────────────
    k8s = {"skipped": True}
    if ENABLE_K8S and CLUSTER_NAME:
        try:
            endpoint, ca = _k8s_api(CLUSTER_NAME)
            ca_path = f"/tmp/{incident_id}-ca.crt"  # nosec B108 — /tmp is the only writable path in Lambda
            with open(ca_path, "wb") as f:
                f.write(ca)
            token = _eks_token(CLUSTER_NAME)
            k8s_events = _k8s_get(endpoint, token, f"/api/v1/namespaces/{namespace}/events?limit=20", ca_path)
            dep = _k8s_get(
                endpoint, token,
                f"/apis/apps/v1/namespaces/{namespace}/deployments/{service}",
                ca_path,
            )
            k8s = {
                "cluster": CLUSTER_NAME,
                "events": k8s_events,
                "deployment": {
                    "name": dep.get("metadata", {}).get("name"),
                    "replicas": dep.get("spec", {}).get("replicas"),
                    "availableReplicas": dep.get("status", {}).get("availableReplicas"),
                    "unavailableReplicas": dep.get("status", {}).get("unavailableReplicas"),
                    "image": (
                        dep.get("spec", {})
                        .get("template", {})
                        .get("spec", {})
                        .get("containers", [{}])[0]
                        .get("image")
                    ),
                },
            }
        except Exception as exc:
            _log("warning", "k8s_enrichment_failed", error=str(exc), cluster=CLUSTER_NAME)
            k8s = {"error": str(exc), "cluster": CLUSTER_NAME}

    bundle = {
        "incident_id": incident_id,
        "dedup_key": dk,
        "received_at": _now(),
        "service": service,
        "env": env,
        "severity": severity,
        "labels": labels,
        "annotations": annotations,
        "prometheus": prom_snapshots,
        "kubernetes": k8s,
        "raw_event": raw,
        "constraints": {"allowed_actions_ref": "gitops/policies/allowed-actions.yaml"},
    }

    key = f"incidents/{incident_id}.json"
    s3.put_object(
        Bucket=INCIDENT_BUCKET,
        Key=key,
        Body=json.dumps(bundle, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    _log("info", "bundle_stored", incident_id=incident_id, s3_key=key)

    event_type = "SentinelPipelineTriggered" if ENABLE_MULTI_AGENT else "SignalBundled"
    _emit(event_type, {
        "incident_id": incident_id,
        "s3_bucket": INCIDENT_BUCKET,
        "s3_key": key,
        "service": service,
        "env": env,
    })
    _log("info", "event_emitted", event_type=event_type, incident_id=incident_id)
    _put_metric("IncidentsReceived", Service=service)
    return {"statusCode": 200, "body": json.dumps({"incident_id": incident_id, "s3_key": key})}
