"""Unit tests for root_cause_agent/app.py"""
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
import root_cause_agent.app as app
importlib.reload(app)

REQUIRED_FIELDS = ("root_cause", "contributing_factors", "affected_components", "diagnosis_confidence")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bundle(service="svc"):
    return {"incident_id": "inc-001", "service": service, "severity": "critical", "labels": {}}


def _triage(incident_type="HighErrorRate", severity="critical"):
    return {"incident_type": incident_type, "severity_class": severity, "blast_radius": "contained"}


def _event(bundle=None, triage=None):
    return {
        "s3_bucket":   "test-bucket",
        "s3_key":      "incidents/inc-001.json",
        "incident_id": "inc-001",
        "triage":      triage or _triage(),
    }


def _mock_s3(bundle):
    return {"Body": MagicMock(read=MagicMock(return_value=json.dumps(bundle).encode()))}


# ---------------------------------------------------------------------------
# Tests: _heuristic_diagnosis() — pure function
# ---------------------------------------------------------------------------

class TestHeuristicDiagnosis(unittest.TestCase):

    def test_oom_killed_maps_to_memory_cause(self):
        result = app._heuristic_diagnosis(_bundle(), _triage("OOMKilled"))
        self.assertIn("memory", result["root_cause"].lower())

    def test_high_error_rate_maps_to_5xx_cause(self):
        result = app._heuristic_diagnosis(_bundle(), _triage("HighErrorRate"))
        self.assertIn("5xx", result["root_cause"].lower())

    def test_pod_crash_loop_mentions_crash(self):
        result = app._heuristic_diagnosis(_bundle(), _triage("PodCrashLoop"))
        self.assertIn("crash", result["root_cause"].lower())

    def test_cpu_throttle_maps_to_throttle_cause(self):
        result = app._heuristic_diagnosis(_bundle(), _triage("CPUThrottle"))
        self.assertIn("throttl", result["root_cause"].lower())

    def test_unknown_type_returns_generic_cause(self):
        result = app._heuristic_diagnosis(_bundle(), _triage("Unknown"))
        self.assertIn("Unknown", result["root_cause"])

    def test_heuristic_confidence_is_conservative(self):
        result = app._heuristic_diagnosis(_bundle(), _triage())
        self.assertEqual(result["diagnosis_confidence"], 30)

    def test_service_in_affected_components(self):
        result = app._heuristic_diagnosis(_bundle("payments-api"), _triage())
        self.assertIn("payments-api", result["affected_components"])

    def test_returns_all_required_fields(self):
        result = app._heuristic_diagnosis(_bundle(), _triage())
        for field in REQUIRED_FIELDS:
            self.assertIn(field, result)


# ---------------------------------------------------------------------------
# Tests: confidence clamping
# ---------------------------------------------------------------------------

class TestConfidenceClamping(unittest.TestCase):

    def _run_handler(self, llm_response):
        b = _bundle()
        with (
            patch.object(app.s3, "get_object", return_value=_mock_s3(b)),
            patch.object(app, "_call_llm", return_value=json.dumps(llm_response)),
        ):
            return app.handler(_event(), MagicMock())

    def test_confidence_above_100_is_clamped(self):
        result = self._run_handler({
            "root_cause": "leak", "contributing_factors": [],
            "affected_components": [], "diagnosis_confidence": 150,
        })
        self.assertLessEqual(result["diagnosis_confidence"], 100)

    def test_confidence_below_0_is_clamped(self):
        result = self._run_handler({
            "root_cause": "unknown", "contributing_factors": [],
            "affected_components": [], "diagnosis_confidence": -10,
        })
        self.assertGreaterEqual(result["diagnosis_confidence"], 0)


# ---------------------------------------------------------------------------
# Tests: handler() fallback and happy path
# ---------------------------------------------------------------------------

class TestHandlerFallback(unittest.TestCase):

    def test_llm_exception_falls_back_to_heuristic(self):
        b = _bundle()
        with (
            patch.object(app.s3, "get_object", return_value=_mock_s3(b)),
            patch.object(app, "_call_llm", side_effect=Exception("timeout")),
        ):
            result = app.handler(_event(), MagicMock())
        for field in REQUIRED_FIELDS:
            self.assertIn(field, result)
        self.assertEqual(result["diagnosis_confidence"], 30)

    def test_llm_missing_field_falls_back_to_heuristic(self):
        b = _bundle()
        with (
            patch.object(app.s3, "get_object", return_value=_mock_s3(b)),
            patch.object(app, "_call_llm", return_value=json.dumps({"root_cause": "partial only"})),
        ):
            result = app.handler(_event(), MagicMock())
        for field in REQUIRED_FIELDS:
            self.assertIn(field, result)

    def test_llm_invalid_json_falls_back_to_heuristic(self):
        b = _bundle()
        with (
            patch.object(app.s3, "get_object", return_value=_mock_s3(b)),
            patch.object(app, "_call_llm", return_value="}{invalid"),
        ):
            result = app.handler(_event(), MagicMock())
        for field in REQUIRED_FIELDS:
            self.assertIn(field, result)


class TestHandlerHappyPath(unittest.TestCase):

    def test_valid_llm_response_is_used(self):
        b = _bundle()
        llm_response = {
            "root_cause":           "Memory leak in payment service",
            "contributing_factors": ["high GC pressure", "missing memory limit"],
            "affected_components":  ["svc"],
            "diagnosis_confidence": 82,
        }
        with (
            patch.object(app.s3, "get_object", return_value=_mock_s3(b)),
            patch.object(app, "_call_llm", return_value=json.dumps(llm_response)),
        ):
            result = app.handler(_event(), MagicMock())
        self.assertEqual(result["root_cause"], "Memory leak in payment service")
        self.assertEqual(result["diagnosis_confidence"], 82)

    def test_markdown_fenced_llm_response_is_used(self):
        b = _bundle()
        llm_response = {
            "root_cause":           "Memory leak in payment service",
            "contributing_factors": ["high GC pressure"],
            "affected_components":  ["svc"],
            "diagnosis_confidence": 82,
        }
        fenced = f"```json\n{json.dumps(llm_response, indent=2)}\n```"
        with (
            patch.object(app.s3, "get_object", return_value=_mock_s3(b)),
            patch.object(app, "_call_llm", return_value=fenced),
        ):
            result = app.handler(_event(), MagicMock())
        self.assertEqual(result["root_cause"], "Memory leak in payment service")
        self.assertEqual(result["diagnosis_confidence"], 82)


if __name__ == "__main__":
    unittest.main()
