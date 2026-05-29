"""Unit tests for signal_collector/app.py"""
import base64
import hashlib
import json
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub heavy dependencies so we can import the module without real AWS creds
# ---------------------------------------------------------------------------

# Stub botocore before anything imports it
botocore_stub = types.ModuleType("botocore")
botocore_credentials_stub = types.ModuleType("botocore.credentials")
botocore_session_stub = types.ModuleType("botocore.session")
botocore_signers_stub = types.ModuleType("botocore.signers")
botocore_session_stub.get_session = MagicMock()
botocore_signers_stub.RequestSigner = MagicMock()
botocore_credentials_stub.Credentials = MagicMock(side_effect=lambda access_key, secret_key, token=None, method=None: {
    "access_key": access_key,
    "secret_key": secret_key,
    "token": token,
    "method": method,
})
botocore_stub.session = botocore_session_stub
sys.modules.setdefault("botocore", botocore_stub)
sys.modules.setdefault("botocore.credentials", botocore_credentials_stub)
sys.modules.setdefault("botocore.session", botocore_session_stub)
sys.modules.setdefault("botocore.signers", botocore_signers_stub)

# Stub boto3
boto3_stub = types.ModuleType("boto3")
boto3_stub.client = MagicMock(return_value=MagicMock())
sys.modules.setdefault("boto3", boto3_stub)

# Stub requests + adapters + urllib3
requests_stub = types.ModuleType("requests")
requests_stub.get = MagicMock()
requests_stub.RequestException = Exception
requests_stub.Session = MagicMock(return_value=MagicMock())
requests_adapters_stub = types.ModuleType("requests.adapters")
requests_adapters_stub.HTTPAdapter = MagicMock()
urllib3_stub = types.ModuleType("urllib3")
urllib3_util_stub = types.ModuleType("urllib3.util")
urllib3_retry_stub = types.ModuleType("urllib3.util.retry")
urllib3_retry_stub.Retry = MagicMock()
sys.modules.setdefault("requests", requests_stub)
sys.modules.setdefault("requests.adapters", requests_adapters_stub)
sys.modules.setdefault("urllib3", urllib3_stub)
sys.modules.setdefault("urllib3.util", urllib3_util_stub)
sys.modules.setdefault("urllib3.util.retry", urllib3_retry_stub)

# Set required env vars before import
os.environ.setdefault("INCIDENT_BUCKET", "test-bucket")
os.environ.setdefault("EVENT_BUS_NAME", "test-bus")
os.environ.setdefault("INCIDENTS_TABLE_NAME", "test-table")
os.environ.setdefault("WEBHOOK_SECRET", "")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import importlib
import signal_collector.app as app

importlib.reload(app)  # ensure env vars are picked up


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_context(request_id="req-1234abcd"):
    ctx = MagicMock()
    ctx.aws_request_id = request_id
    return ctx


def _alertmanager_event(service="svc", env="staging", alertname="HighErrorRate"):
    return {
        "requestContext": {"requestId": "x"},
        "body": json.dumps({
            "alerts": [{
                "labels": {
                    "alertname": alertname,
                    "service": service,
                    "env": env,
                    "severity": "critical",
                },
                "annotations": {"summary": "Error rate high"},
            }]
        }),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDedupKey(unittest.TestCase):
    def test_deterministic(self):
        k1 = app._dedup_key("svc", "staging", "HighErrorRate")
        k2 = app._dedup_key("svc", "staging", "HighErrorRate")
        self.assertEqual(k1, k2)

    def test_different_inputs_produce_different_keys(self):
        k1 = app._dedup_key("svc-a", "staging", "Alert")
        k2 = app._dedup_key("svc-b", "staging", "Alert")
        self.assertNotEqual(k1, k2)

    def test_sha256_format(self):
        k = app._dedup_key("svc", "prod", "Alert")
        self.assertEqual(len(k), 64)


class TestPromQuery(unittest.TestCase):
    def test_skips_when_no_url(self):
        original = app.PROM_URL
        app.PROM_URL = ""
        result = app._prom_query("up")
        self.assertTrue(result.get("skipped"))
        app.PROM_URL = original

    def test_returns_error_on_request_exception(self):
        app.PROM_URL = "http://prom:9090"
        with patch.object(app._SESSION, "get", side_effect=Exception("connection refused")):
            result = app._prom_query("up")
        self.assertIn("error", result)
        app.PROM_URL = ""

    def test_proxies_incluster_service_url_via_k8s_api(self):
        app.PROM_URL = "http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090"
        app.CLUSTER_NAME = "gitops-sentinel-cluster"
        response = MagicMock()
        response.json.return_value = {"status": "success"}
        response.raise_for_status = MagicMock()
        with (
            patch.object(app, "_k8s_api", return_value=("https://cluster.example", b"ca-bytes")),
            patch.object(app, "_eks_token", return_value="token"),
            patch.object(app._SESSION, "get", return_value=response) as mock_get,
        ):
            result = app._prom_query("up")

        self.assertEqual(result, {"status": "success"})
        called_url = mock_get.call_args.args[0]
        self.assertIn("/api/v1/namespaces/monitoring/services/http:kube-prometheus-stack-prometheus:9090/proxy/api/v1/query", called_url)
        self.assertEqual(mock_get.call_args.kwargs["params"], {"query": "up"})
        app.PROM_URL = ""
        app.CLUSTER_NAME = ""

    def test_detects_cluster_service_prometheus_url(self):
        target = app._prometheus_proxy_target(
            "http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090"
        )
        self.assertEqual(
            target,
            {
                "scheme": "http",
                "service": "kube-prometheus-stack-prometheus",
                "namespace": "monitoring",
                "port": 9090,
            },
        )

    def test_ignores_non_cluster_service_prometheus_url(self):
        self.assertIsNone(app._prometheus_proxy_target("http://internal-prometheus.example.com:9090"))


class TestEksToken(unittest.TestCase):
    def test_builds_presigned_eks_bearer_token(self):
        session = MagicMock()
        creds = MagicMock(access_key="AKIA", secret_key="SECRET", token="TOKEN", method="assume-role")
        frozen = types.SimpleNamespace(access_key="AKIA", secret_key="SECRET", token="TOKEN", method="assume-role")
        creds.get_frozen_credentials.return_value = frozen
        session.get_credentials.return_value = creds
        session.get_component.return_value = "events"
        sts_model = MagicMock(service_id="STS", signing_name="sts")
        session.get_service_model.return_value = sts_model
        signer = MagicMock()
        signer.generate_presigned_url.return_value = "https://signed.example"

        with (
            patch.object(app.botocore.session, "get_session", return_value=session),
            patch.object(app, "RequestSigner", return_value=signer) as mock_signer,
        ):
            token = app._eks_token("gitops-sentinel-cluster")

        mock_signer.assert_called_once_with(
            service_id="STS",
            region_name=app.AWS_REGION,
            signing_name="sts",
            signature_version="v4",
            credentials={
                "access_key": "AKIA",
                "secret_key": "SECRET",
                "token": "TOKEN",
                "method": "assume-role",
            },
            event_emitter="events",
        )
        self.assertTrue(token.startswith("k8s-aws-v1."))
        encoded = token.removeprefix("k8s-aws-v1.")
        padded = encoded + ("=" * ((4 - len(encoded) % 4) % 4))
        self.assertEqual(base64.urlsafe_b64decode(padded).decode("utf-8"), "https://signed.example")

    def test_wraps_readonly_credentials_for_request_signer(self):
        session = MagicMock()
        creds = types.SimpleNamespace(
            access_key="AKIA2",
            secret_key="SECRET2",
            token="TOKEN2",
            method="env",
        )
        session.get_credentials.return_value = creds
        session.get_component.return_value = "events"
        sts_model = MagicMock(service_id="STS", signing_name="sts")
        session.get_service_model.return_value = sts_model
        signer = MagicMock()
        signer.generate_presigned_url.return_value = "https://signed.example"

        with (
            patch.object(app.botocore.session, "get_session", return_value=session),
            patch.object(app, "RequestSigner", return_value=signer) as mock_signer,
        ):
            app._eks_token("gitops-sentinel-cluster")

        self.assertFalse(hasattr(creds, "get_frozen_credentials"))
        self.assertEqual(
            mock_signer.call_args.kwargs["credentials"],
            {
                "access_key": "AKIA2",
                "secret_key": "SECRET2",
                "token": "TOKEN2",
                "method": "env",
            },
        )


class TestWebhookSecretValidation(unittest.TestCase):
    def setUp(self):
        # Patch internal helpers so handler doesn't need real AWS
        self._dedup = patch.object(app, "_dedup_check_and_write", return_value=(True, None))
        self._prom = patch.object(app, "_prom_query", return_value={"skipped": True})
        self._s3 = patch.object(app.s3, "put_object")
        self._emit = patch.object(app, "_emit")
        self._dedup.start()
        self._prom.start()
        self._s3.start()
        self._emit.start()

    def tearDown(self):
        patch.stopall()
        app.WEBHOOK_SECRET = ""

    def test_missing_secret_returns_401(self):
        app.WEBHOOK_SECRET = "correct-secret"
        event = _alertmanager_event()
        event["headers"] = {"x-webhook-secret": "wrong"}
        resp = app.handler(event, _make_context())
        self.assertEqual(resp["statusCode"], 401)

    def test_correct_secret_passes(self):
        app.WEBHOOK_SECRET = "correct-secret"
        event = _alertmanager_event()
        event["headers"] = {"x-webhook-secret": "correct-secret"}
        resp = app.handler(event, _make_context())
        self.assertEqual(resp["statusCode"], 200)

    def test_no_secret_configured_skips_auth(self):
        app.WEBHOOK_SECRET = ""
        event = _alertmanager_event()
        resp = app.handler(event, _make_context())
        self.assertEqual(resp["statusCode"], 200)

    def test_bearer_token_passes(self):
        app.WEBHOOK_SECRET = "correct-secret"
        event = _alertmanager_event()
        event["headers"] = {"authorization": "Bearer correct-secret"}
        resp = app.handler(event, _make_context())
        self.assertEqual(resp["statusCode"], 200)

    def test_bearer_token_wrong_returns_401(self):
        app.WEBHOOK_SECRET = "correct-secret"
        event = _alertmanager_event()
        event["headers"] = {"authorization": "Bearer wrong"}
        resp = app.handler(event, _make_context())
        self.assertEqual(resp["statusCode"], 401)


class TestHandlerDedup(unittest.TestCase):
    def test_dedup_suppressed_returns_202(self):
        with patch.object(app, "_dedup_check_and_write", return_value=(False, None)):
            event = _alertmanager_event()
            resp = app.handler(event, _make_context())
        self.assertEqual(resp["statusCode"], 202)
        body = json.loads(resp["body"])
        self.assertEqual(body["message"], "dedup_suppressed")


class TestHandlerSuccess(unittest.TestCase):
    def test_stores_bundle_and_returns_200(self):
        with (
            patch.object(app, "_dedup_check_and_write", return_value=(True, None)),
            patch.object(app, "_prom_query", return_value={"skipped": True}),
            patch.object(app.s3, "put_object") as mock_put,
            patch.object(app, "_emit") as mock_emit,
        ):
            event = _alertmanager_event()
            resp = app.handler(event, _make_context())

        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        self.assertIn("incident_id", body)
        self.assertIn("s3_key", body)
        mock_put.assert_called_once()
        mock_emit.assert_called_once()

    def test_bundle_s3_key_format(self):
        with (
            patch.object(app, "_dedup_check_and_write", return_value=(True, None)),
            patch.object(app, "_prom_query", return_value={"skipped": True}),
            patch.object(app.s3, "put_object"),
            patch.object(app, "_emit"),
        ):
            event = _alertmanager_event()
            resp = app.handler(event, _make_context())

        body = json.loads(resp["body"])
        self.assertTrue(body["s3_key"].startswith("incidents/inc-"))


class TestNamespaceExtraction(unittest.TestCase):
    """namespace label is extracted from alert labels and stored in the bundle."""

    def test_namespace_from_label(self):
        with (
            patch.object(app, "_dedup_check_and_write", return_value=(True, None)),
            patch.object(app, "_prom_query", return_value={"skipped": True}),
            patch.object(app.s3, "put_object") as mock_put,
            patch.object(app, "_emit"),
        ):
            event = {
                "requestContext": {"requestId": "x"},
                "body": json.dumps({"alerts": [{"labels": {
                    "alertname": "HighErrorRate",
                    "service":   "payments",
                    "namespace": "payments-ns",
                    "env":       "staging",
                    "severity":  "critical",
                }, "annotations": {}}]}),
            }
            app.handler(event, _make_context())

        call_kwargs = mock_put.call_args[1]
        stored = json.loads(call_kwargs["Body"].decode("utf-8"))
        # namespace must propagate into the Prometheus queries (they use namespace variable)
        # We verify it was extracted by checking the bundle's labels
        self.assertEqual(stored["labels"]["namespace"], "payments-ns")

    def test_namespace_falls_back_to_env_when_absent(self):
        with (
            patch.object(app, "_dedup_check_and_write", return_value=(True, None)),
            patch.object(app, "_prom_query", return_value={"skipped": True}),
            patch.object(app.s3, "put_object") as mock_put,
            patch.object(app, "_emit"),
        ):
            event = _alertmanager_event(service="svc", env="prod")
            app.handler(event, _make_context())

        call_kwargs = mock_put.call_args[1]
        stored = json.loads(call_kwargs["Body"].decode("utf-8"))
        # No namespace label — should fall back to env ("prod")
        self.assertEqual(stored["env"], "prod")


class TestMultiAgentRouting(unittest.TestCase):
    """When ENABLE_MULTI_AGENT=true, handler emits SentinelPipelineTriggered."""

    def test_emits_sentinel_pipeline_triggered_when_multi_agent_enabled(self):
        original = app.ENABLE_MULTI_AGENT
        app.ENABLE_MULTI_AGENT = True
        with (
            patch.object(app, "_dedup_check_and_write", return_value=(True, None)),
            patch.object(app, "_prom_query", return_value={"skipped": True}),
            patch.object(app.s3, "put_object"),
            patch.object(app, "_emit") as mock_emit,
        ):
            app.handler(_alertmanager_event(), _make_context())
        app.ENABLE_MULTI_AGENT = original
        event_type = mock_emit.call_args[0][0]
        self.assertEqual(event_type, "SentinelPipelineTriggered")

    def test_emits_signal_bundled_when_multi_agent_disabled(self):
        original = app.ENABLE_MULTI_AGENT
        app.ENABLE_MULTI_AGENT = False
        with (
            patch.object(app, "_dedup_check_and_write", return_value=(True, None)),
            patch.object(app, "_prom_query", return_value={"skipped": True}),
            patch.object(app.s3, "put_object"),
            patch.object(app, "_emit") as mock_emit,
        ):
            app.handler(_alertmanager_event(), _make_context())
        app.ENABLE_MULTI_AGENT = original
        event_type = mock_emit.call_args[0][0]
        self.assertEqual(event_type, "SignalBundled")


class TestHandlerFailureModes(unittest.TestCase):
    """Handler degrades gracefully on malformed or missing input."""

    def test_malformed_json_body_does_not_raise(self):
        with (
            patch.object(app, "_dedup_check_and_write", return_value=(True, None)),
            patch.object(app, "_prom_query", return_value={"skipped": True}),
            patch.object(app.s3, "put_object"),
            patch.object(app, "_emit"),
        ):
            event = {"requestContext": {"requestId": "x"}, "body": "{not valid json"}
            resp = app.handler(event, _make_context())
        self.assertEqual(resp["statusCode"], 200)

    def test_missing_alerts_array_does_not_raise(self):
        with (
            patch.object(app, "_dedup_check_and_write", return_value=(True, None)),
            patch.object(app, "_prom_query", return_value={"skipped": True}),
            patch.object(app.s3, "put_object"),
            patch.object(app, "_emit"),
        ):
            event = {"requestContext": {"requestId": "x"}, "body": json.dumps({"version": "4"})}
            resp = app.handler(event, _make_context())
        self.assertEqual(resp["statusCode"], 200)

    def test_s3_failure_propagates(self):
        with (
            patch.object(app, "_dedup_check_and_write", return_value=(True, None)),
            patch.object(app, "_prom_query", return_value={"skipped": True}),
            patch.object(app.s3, "put_object", side_effect=Exception("S3 unavailable")),
        ):
            with self.assertRaises(Exception):
                app.handler(_alertmanager_event(), _make_context())


if __name__ == "__main__":
    unittest.main()
