"""Unit tests for classifier_agent/app.py"""
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import importlib
import classifier_agent.app as app
importlib.reload(app)

REQUIRED_FIELDS = ("severity_class", "incident_type", "blast_radius", "priority", "key_signals")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bundle(severity="critical", alertname="HighErrorRate", service="svc"):
    return {
        "incident_id": "inc-001",
        "service":     service,
        "severity":    severity,
        "labels":      {"alertname": alertname, "severity": severity},
    }


def _s3_event():
    return {"s3_bucket": "test-bucket", "s3_key": "incidents/inc-001.json", "incident_id": "inc-001"}


def _mock_s3(bundle):
    return {"Body": MagicMock(read=MagicMock(return_value=json.dumps(bundle).encode()))}


# ---------------------------------------------------------------------------
# Tests: _heuristic_triage() — pure function
# ---------------------------------------------------------------------------

class TestHeuristicTriage(unittest.TestCase):

    def test_critical_severity_maps_correctly(self):
        result = app._heuristic_triage(_bundle("critical"))
        self.assertEqual(result["severity_class"], "critical")
        self.assertEqual(result["priority"], 2)

    def test_warning_severity_maps_to_high(self):
        result = app._heuristic_triage(_bundle("warning"))
        self.assertEqual(result["severity_class"], "high")
        self.assertEqual(result["priority"], 3)

    def test_info_severity_maps_to_medium(self):
        result = app._heuristic_triage(_bundle("info"))
        self.assertEqual(result["severity_class"], "medium")

    def test_unknown_severity_defaults_to_medium(self):
        result = app._heuristic_triage(_bundle("unknown_value"))
        self.assertEqual(result["severity_class"], "medium")

    def test_alertname_appears_in_key_signals(self):
        result = app._heuristic_triage(_bundle(alertname="PodCrashLoop"))
        self.assertTrue(any("PodCrashLoop" in s for s in result["key_signals"]))

    def test_returns_all_required_fields(self):
        result = app._heuristic_triage(_bundle())
        for field in REQUIRED_FIELDS:
            self.assertIn(field, result)

    def test_key_signals_is_a_list(self):
        result = app._heuristic_triage(_bundle())
        self.assertIsInstance(result["key_signals"], list)


# ---------------------------------------------------------------------------
# Tests: handler() fallback behaviour
# ---------------------------------------------------------------------------

class TestHandlerFallback(unittest.TestCase):

    def test_llm_exception_falls_back_to_heuristic(self):
        b = _bundle("critical")
        with (
            patch.object(app.s3, "get_object", return_value=_mock_s3(b)),
            patch.object(app, "_call_llm", side_effect=Exception("throttled")),
        ):
            result = app.handler(_s3_event(), MagicMock())
        for field in REQUIRED_FIELDS:
            self.assertIn(field, result)

    def test_llm_missing_field_falls_back_to_heuristic(self):
        b = _bundle("warning")
        with (
            patch.object(app.s3, "get_object", return_value=_mock_s3(b)),
            patch.object(app, "_call_llm", return_value=json.dumps({"severity_class": "high"})),
        ):
            result = app.handler(_s3_event(), MagicMock())
        # Heuristic fills in all fields
        for field in REQUIRED_FIELDS:
            self.assertIn(field, result)

    def test_llm_invalid_json_falls_back_to_heuristic(self):
        b = _bundle()
        with (
            patch.object(app.s3, "get_object", return_value=_mock_s3(b)),
            patch.object(app, "_call_llm", return_value="not-valid-json{{{"),
        ):
            result = app.handler(_s3_event(), MagicMock())
        for field in REQUIRED_FIELDS:
            self.assertIn(field, result)


# ---------------------------------------------------------------------------
# Tests: handler() happy path
# ---------------------------------------------------------------------------

class TestHandlerHappyPath(unittest.TestCase):

    def test_valid_llm_response_is_used(self):
        b = _bundle("critical")
        llm_response = {
            "severity_class": "critical",
            "incident_type":  "HighErrorRate",
            "blast_radius":   "broad",
            "priority":       1,
            "key_signals":    ["error_rate=0.9"],
        }
        with (
            patch.object(app.s3, "get_object", return_value=_mock_s3(b)),
            patch.object(app, "_call_llm", return_value=json.dumps(llm_response)),
        ):
            result = app.handler(_s3_event(), MagicMock())
        self.assertEqual(result["severity_class"], "critical")
        self.assertEqual(result["incident_type"],  "HighErrorRate")
        self.assertEqual(result["priority"],       1)

    def test_markdown_fenced_llm_response_is_used(self):
        b = _bundle("critical")
        llm_response = {
            "severity_class": "critical",
            "incident_type":  "HighErrorRate",
            "blast_radius":   "broad",
            "priority":       1,
            "key_signals":    ["error_rate=0.9"],
        }
        fenced = f"```json\n{json.dumps(llm_response, indent=2)}\n```"
        with (
            patch.object(app.s3, "get_object", return_value=_mock_s3(b)),
            patch.object(app, "_call_llm", return_value=fenced),
        ):
            result = app.handler(_s3_event(), MagicMock())
        self.assertEqual(result["incident_type"], "HighErrorRate")
        self.assertEqual(result["priority"],      1)


if __name__ == "__main__":
    unittest.main()
