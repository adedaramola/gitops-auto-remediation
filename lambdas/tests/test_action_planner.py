"""Unit tests for action_planner/app.py"""
import json
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub heavy dependencies before import
# ---------------------------------------------------------------------------

boto3_stub = types.ModuleType("boto3")
boto3_stub.client = MagicMock(return_value=MagicMock())
sys.modules.setdefault("boto3", boto3_stub)

requests_stub = types.ModuleType("requests")
requests_stub.Session = MagicMock(return_value=MagicMock())
requests_stub.RequestException = Exception
requests_adapters_stub = types.ModuleType("requests.adapters")
requests_adapters_stub.HTTPAdapter = MagicMock()
urllib3_stub        = types.ModuleType("urllib3")
urllib3_util_stub   = types.ModuleType("urllib3.util")
urllib3_retry_stub  = types.ModuleType("urllib3.util.retry")
urllib3_retry_stub.Retry = MagicMock()
sys.modules.setdefault("requests",             requests_stub)
sys.modules.setdefault("requests.adapters",    requests_adapters_stub)
sys.modules.setdefault("urllib3",              urllib3_stub)
sys.modules.setdefault("urllib3.util",         urllib3_util_stub)
sys.modules.setdefault("urllib3.util.retry",   urllib3_retry_stub)

# yaml (PyYAML) is a real installed dependency — no stub needed.

os.environ.setdefault("GITHUB_OWNER",               "test-org")
os.environ.setdefault("GITHUB_REPO",                "test-repo")
os.environ.setdefault("GITHUB_APP_TOKEN_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:123:secret:gh")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import importlib
import action_planner.app as app
importlib.reload(app)

REQUIRED_FIELDS = ("action", "params", "target", "reasoning")
_ALLOWED = {"allowed_actions": [
    {"action": "rollback_image"},
    {"action": "scale_replicas"},
    {"action": "restart_rollout"},
    {"action": "tune_resources"},
]}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bundle(service="svc", env="staging"):
    return {
        "incident_id": "inc-001",
        "service":     service,
        "env":         env,
        "labels":      {},
        "prometheus":  {},
    }


def _event(bundle=None):
    b = bundle or _bundle()
    return {
        "s3_bucket":   "test-bucket",
        "s3_key":      "incidents/inc-001.json",
        "incident_id": "inc-001",
        "triage":      {"severity_class": "high", "blast_radius": "contained"},
        "diagnosis":   {"root_cause": "OOM", "diagnosis_confidence": 70},
    }


def _mock_s3(bundle):
    return {"Body": MagicMock(read=MagicMock(return_value=json.dumps(bundle).encode()))}


# ---------------------------------------------------------------------------
# Tests: heuristic fallback
# ---------------------------------------------------------------------------

class TestHandlerHeuristicFallback(unittest.TestCase):

    def _run(self, bundle=None, allowed=None, llm_exc=Exception("timeout")):
        b = bundle or _bundle()
        a = allowed if allowed is not None else _ALLOWED
        with (
            patch.object(app.s3, "get_object", return_value=_mock_s3(b)),
            patch.object(app, "_fetch_allowed_actions", return_value=a),
            patch.object(app, "_call_llm", side_effect=llm_exc),
        ):
            return app.handler(_event(b), MagicMock())

    def test_llm_timeout_returns_heuristic_with_all_fields(self):
        result = self._run()
        for field in REQUIRED_FIELDS:
            self.assertIn(field, result)

    def test_heuristic_defaults_to_restart_rollout(self):
        result = self._run()
        self.assertEqual(result["action"], "restart_rollout")

    def test_heuristic_uses_service_from_bundle(self):
        result = self._run(bundle=_bundle(service="payments-api"))
        self.assertEqual(result["target"]["service"], "payments-api")

    def test_heuristic_uses_env_from_bundle(self):
        result = self._run(bundle=_bundle(env="prod"))
        self.assertEqual(result["target"]["env"], "prod")

    def test_empty_allowed_actions_still_returns_result(self):
        result = self._run(allowed={"allowed_actions": []})
        self.assertIn("action", result)

    def test_fetch_allowed_actions_failure_uses_defaults(self):
        b = _bundle()
        with (
            patch.object(app.s3, "get_object", return_value=_mock_s3(b)),
            patch.object(app, "_fetch_allowed_actions", side_effect=Exception("network error")),
            patch.object(app, "_call_llm", side_effect=Exception("also fails")),
        ):
            result = app.handler(_event(b), MagicMock())
        self.assertIn("action", result)


# ---------------------------------------------------------------------------
# Tests: action validation
# ---------------------------------------------------------------------------

class TestHandlerActionValidation(unittest.TestCase):

    def test_disallowed_action_falls_back_to_heuristic(self):
        b = _bundle()
        with (
            patch.object(app.s3, "get_object", return_value=_mock_s3(b)),
            patch.object(app, "_fetch_allowed_actions", return_value=_ALLOWED),
            patch.object(app, "_call_llm", return_value=json.dumps({
                "action":       "delete_namespace",
                "params":       {},
                "target":       {"service": "svc", "env": "staging"},
                "reasoning":    "nope",
                "alternatives": [],
            })),
        ):
            result = app.handler(_event(b), MagicMock())
        self.assertNotEqual(result["action"], "delete_namespace")
        self.assertIn(result["action"], [a["action"] for a in _ALLOWED["allowed_actions"]] + ["restart_rollout"])

    def test_valid_llm_action_is_returned(self):
        b = _bundle()
        with (
            patch.object(app.s3, "get_object", return_value=_mock_s3(b)),
            patch.object(app, "_fetch_allowed_actions", return_value=_ALLOWED),
            patch.object(app, "_call_llm", return_value=json.dumps({
                "action":       "rollback_image",
                "params":       {"tag": "stable"},
                "target":       {"service": "svc", "env": "staging"},
                "reasoning":    "Bad deploy detected in Prometheus",
                "alternatives": ["scale_replicas"],
            })),
        ):
            result = app.handler(_event(b), MagicMock())
        self.assertEqual(result["action"], "rollback_image")

    def test_markdown_fenced_llm_action_is_returned(self):
        b = _bundle()
        plan = {
            "action":       "rollback_image",
            "params":       {"tag": "stable"},
            "target":       {"service": "svc", "env": "staging"},
            "reasoning":    "Bad deploy detected in Prometheus",
            "alternatives": ["scale_replicas"],
        }
        fenced = f"```json\n{json.dumps(plan, indent=2)}\n```"
        with (
            patch.object(app.s3, "get_object", return_value=_mock_s3(b)),
            patch.object(app, "_fetch_allowed_actions", return_value=_ALLOWED),
            patch.object(app, "_call_llm", return_value=fenced),
        ):
            result = app.handler(_event(b), MagicMock())
        self.assertEqual(result["action"], "rollback_image")

    def test_llm_missing_required_field_falls_back(self):
        b = _bundle()
        with (
            patch.object(app.s3, "get_object", return_value=_mock_s3(b)),
            patch.object(app, "_fetch_allowed_actions", return_value=_ALLOWED),
            patch.object(app, "_call_llm", return_value=json.dumps({
                "action": "scale_replicas",
                # missing params, target, reasoning
            })),
        ):
            result = app.handler(_event(b), MagicMock())
        for field in REQUIRED_FIELDS:
            self.assertIn(field, result)


if __name__ == "__main__":
    unittest.main()
