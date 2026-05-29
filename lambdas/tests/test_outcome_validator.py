"""Unit tests for outcome_validator/app.py"""
import json
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Stub dependencies before import
# ---------------------------------------------------------------------------

# Stub botocore for EKS auth helpers
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

boto3_stub = types.ModuleType("boto3")
boto3_stub.client = MagicMock(return_value=MagicMock())
sys.modules.setdefault("boto3", boto3_stub)

requests_stub = types.ModuleType("requests")
requests_stub.get = MagicMock()
requests_stub.post = MagicMock()
requests_stub.request = MagicMock()
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

os.environ.setdefault("GITHUB_OWNER", "test-org")
os.environ.setdefault("GITHUB_REPO", "test-repo")
os.environ.setdefault("GITHUB_APP_TOKEN_SECRET_ARN", "")
os.environ.setdefault("EVENT_BUS_NAME", "test-bus")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import importlib
import outcome_validator.app as app

importlib.reload(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _eb_event(incident_id="inc-123", service="demo-service"):
    return {"detail": {"incident_id": incident_id, "service": service}}


def _prom_response(error_rate: float):
    return {"data": {"result": [{"value": [0, str(error_rate)]}]}}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestExtractIncidentId(unittest.TestCase):
    def test_reads_incident_id(self):
        self.assertEqual(app._extract_incident_id({"incident_id": "inc-42"}), "inc-42")

    def test_falls_back_to_inc(self):
        self.assertEqual(app._extract_incident_id({"inc": "inc-99"}), "inc-99")

    def test_returns_unknown_when_missing(self):
        self.assertEqual(app._extract_incident_id({}), "unknown")


class TestPromQuery(unittest.TestCase):
    def test_skips_when_no_url(self):
        original = app.PROM_URL
        app.PROM_URL = ""
        result = app._prom_query("up")
        self.assertTrue(result.get("skipped"))
        app.PROM_URL = original

    def test_returns_error_on_exception(self):
        app.PROM_URL = "http://prom:9090"
        with patch.object(app._SESSION, "get", side_effect=Exception("timeout")):
            result = app._prom_query("up")
        self.assertIn("error", result)
        app.PROM_URL = ""

    def test_proxies_incluster_service_url_via_k8s_api(self):
        app.PROM_URL = "http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090"
        os.environ["CLUSTER_NAME"] = "gitops-sentinel-cluster"
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
        os.environ.pop("CLUSTER_NAME", None)

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


class TestSlack(unittest.TestCase):
    def test_skips_when_no_url(self):
        app.SLACK_WEBHOOK_URL = ""
        app._slack("hello")
        requests_stub.post.assert_not_called()

    def test_posts_when_url_set(self):
        app.SLACK_WEBHOOK_URL = "https://hooks.slack.com/x"
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch.object(app._SESSION, "post", return_value=mock_resp) as mock_post:
            app._slack("test message")
            mock_post.assert_called_once()
        app.SLACK_WEBHOOK_URL = ""


class TestHandlerRecovered(unittest.TestCase):
    """When error rate is below threshold, emit OutcomeValidated."""

    def test_verified_when_low_error_rate(self):
        with (
            patch.object(app, "_prom_query", return_value=_prom_response(0.05)),
            patch.object(app, "_emit") as mock_emit,
            patch.object(app, "_slack"),
        ):
            resp = app.handler(_eb_event(), MagicMock())

        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        self.assertTrue(body["recovered"])
        mock_emit.assert_called_once_with("OutcomeValidated", unittest.mock.ANY)

    def test_failed_when_high_error_rate(self):
        app.AUTO_REVERT_ON_FAIL = False  # disable revert for this test
        with (
            patch.object(app, "_prom_query", return_value=_prom_response(0.9)),
            patch.object(app, "_emit") as mock_emit,
            patch.object(app, "_slack"),
        ):
            resp = app.handler(_eb_event(), MagicMock())

        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        self.assertFalse(body["recovered"])
        mock_emit.assert_called_once_with("OutcomeFailed", unittest.mock.ANY)
        app.AUTO_REVERT_ON_FAIL = True


class TestHandlerPromSkipped(unittest.TestCase):
    """When Prometheus is not configured, recovered=False (safe default)."""

    def test_defaults_to_not_recovered(self):
        app.AUTO_REVERT_ON_FAIL = False
        with (
            patch.object(app, "_prom_query", return_value={"skipped": True}),
            patch.object(app, "_emit"),
            patch.object(app, "_slack"),
        ):
            resp = app.handler(_eb_event(), MagicMock())

        body = json.loads(resp["body"])
        self.assertFalse(body["recovered"])
        app.AUTO_REVERT_ON_FAIL = True


class TestAutoRevert(unittest.TestCase):
    """Auto-revert opens a revert PR when remediation fails."""

    def test_revert_pr_url_in_response(self):
        app.AUTO_REVERT_ON_FAIL = True
        app.GITHUB_OWNER = "org"
        app.GITHUB_REPO = "repo"
        app.GITHUB_TOKEN_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123:secret:gh"

        revert_result = {"revert_pr_url": "https://github.com/org/repo/pull/99", "restored": []}
        with (
            patch.object(app, "_prom_query", return_value=_prom_response(0.9)),
            patch.object(app, "_get_secret_json", return_value={"token": "ghp_test"}),
            patch.object(app, "_auto_revert", return_value=revert_result),
            patch.object(app, "_emit"),
            patch.object(app, "_slack") as mock_slack,
        ):
            resp = app.handler(_eb_event(), MagicMock())

        body = json.loads(resp["body"])
        self.assertIn("revert_pr_url", body["revert"])
        # Slack message should contain the revert PR URL
        slack_msg = mock_slack.call_args[0][0]
        self.assertIn("https://github.com/org/repo/pull/99", slack_msg)


if __name__ == "__main__":
    unittest.main()
