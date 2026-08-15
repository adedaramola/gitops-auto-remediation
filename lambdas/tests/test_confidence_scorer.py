"""Unit tests for confidence_scorer/app.py"""
import os
import sys
import types
import unittest
from unittest.mock import MagicMock

# Stub boto3 — confidence_scorer imports it at module level
boto3_stub = types.ModuleType("boto3")
boto3_stub.client = MagicMock(return_value=MagicMock())
sys.modules.setdefault("boto3", boto3_stub)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import importlib
import confidence_scorer.app as app
importlib.reload(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _triage(severity="medium", blast="contained"):
    return {"severity_class": severity, "blast_radius": blast}


def _diagnosis(confidence=70):
    return {"diagnosis_confidence": confidence}


def _remediation(action="scale_replicas"):
    return {"action": action}


def _event(severity="medium", blast="contained", action="scale_replicas", confidence=70):
    return {
        "incident_id": "inc-test-001",
        "triage":      _triage(severity, blast),
        "diagnosis":   _diagnosis(confidence),
        "remediation": _remediation(action),
    }


# ---------------------------------------------------------------------------
# Tests: _score() — deterministic scoring table
# ---------------------------------------------------------------------------

class TestScore(unittest.TestCase):

    def test_high_base_isolated_safe_action_scores_above_80(self):
        score, risk, _ = app._score(_triage("low", "isolated"), _diagnosis(90), _remediation("scale_replicas"))
        self.assertGreaterEqual(score, 80)
        self.assertEqual(risk, "low")

    def test_critical_severity_lowers_score(self):
        score_crit, _, _ = app._score(_triage("critical", "isolated"), _diagnosis(90), _remediation("scale_replicas"))
        score_low,  _, _ = app._score(_triage("low",      "isolated"), _diagnosis(90), _remediation("scale_replicas"))
        self.assertLess(score_crit, score_low)

    def test_broad_blast_radius_lowers_score(self):
        score_broad,    _, _ = app._score(_triage("medium", "broad"),    _diagnosis(70), _remediation("scale_replicas"))
        score_isolated, _, _ = app._score(_triage("medium", "isolated"), _diagnosis(70), _remediation("scale_replicas"))
        self.assertLess(score_broad, score_isolated)

    def test_score_clamped_to_0(self):
        score, _, _ = app._score(_triage("critical", "broad"), _diagnosis(0), _remediation("rollback_image"))
        self.assertGreaterEqual(score, 0)

    def test_score_clamped_to_100(self):
        score, _, _ = app._score(_triage("low", "isolated"), _diagnosis(100), _remediation("scale_replicas"))
        self.assertLessEqual(score, 100)

    def test_low_diagnosis_confidence_flagged_in_factors(self):
        _, _, factors = app._score(_triage("medium", "contained"), _diagnosis(30), _remediation("scale_replicas"))
        self.assertTrue(any("low_diagnosis_confidence" in f for f in factors))

    def test_critical_rollback_forces_high_risk(self):
        _, risk, factors = app._score(_triage("critical", "isolated"), _diagnosis(90), _remediation("rollback_image"))
        self.assertEqual(risk, "high")
        self.assertTrue(any("forced_high_risk" in f for f in factors))

    def test_critical_tune_resources_forces_high_risk(self):
        _, risk, _ = app._score(_triage("critical", "isolated"), _diagnosis(90), _remediation("tune_resources"))
        self.assertEqual(risk, "high")

    def test_high_severity_non_critical_does_not_force_high_risk(self):
        # "high" severity is not in _HIGH_RISK_ACTIONS trigger — risk_level driven by score only
        _, risk, _ = app._score(_triage("high", "isolated"), _diagnosis(90), _remediation("rollback_image"))
        # score = 90 - 15(high) - 0(isolated) - 10(rollback) = 65 → medium risk
        self.assertNotEqual(risk, "low")

    def test_unknown_severity_uses_fallback_penalty(self):
        # Should not raise; unknown severity gets the default penalty
        score, _, _ = app._score(_triage("unknown_val", "isolated"), _diagnosis(70), _remediation("scale_replicas"))
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)


# ---------------------------------------------------------------------------
# Tests: _recommend() — routing thresholds
# ---------------------------------------------------------------------------

class TestRecommend(unittest.TestCase):

    def test_score_80_low_risk_yields_auto_apply(self):
        self.assertEqual(app._recommend(80, "low"), "auto_apply")

    def test_score_above_80_but_high_risk_yields_open_pr(self):
        self.assertEqual(app._recommend(85, "high"), "open_pr")

    def test_score_above_80_but_medium_risk_yields_open_pr(self):
        self.assertEqual(app._recommend(82, "medium"), "open_pr")

    def test_score_79_yields_open_pr(self):
        self.assertEqual(app._recommend(79, "low"), "open_pr")

    def test_score_40_yields_open_pr(self):
        self.assertEqual(app._recommend(40, "medium"), "open_pr")

    def test_score_39_yields_escalate(self):
        self.assertEqual(app._recommend(39, "high"), "escalate")

    def test_score_0_yields_escalate(self):
        self.assertEqual(app._recommend(0, "high"), "escalate")


# ---------------------------------------------------------------------------
# Tests: handler() — end-to-end routing
# ---------------------------------------------------------------------------

class TestHandler(unittest.TestCase):

    def test_returns_all_required_fields(self):
        result = app.handler(_event(), MagicMock())
        for field in ("confidence_score", "risk_level", "risk_factors", "recommendation"):
            self.assertIn(field, result)

    def test_recommendation_is_one_of_three_valid_values(self):
        result = app.handler(_event(), MagicMock())
        self.assertIn(result["recommendation"], ("auto_apply", "open_pr", "escalate"))

    def test_empty_event_does_not_raise(self):
        result = app.handler({}, MagicMock())
        self.assertIn("recommendation", result)

    def test_low_confidence_critical_routes_to_escalate(self):
        result = app.handler(_event(severity="critical", blast="broad", confidence=10), MagicMock())
        self.assertEqual(result["recommendation"], "escalate")

    def test_high_confidence_safe_routes_to_auto_apply(self):
        result = app.handler(
            _event(severity="low", blast="isolated", action="scale_replicas", confidence=90),
            MagicMock(),
        )
        self.assertEqual(result["recommendation"], "auto_apply")

    def test_medium_confidence_routes_to_open_pr(self):
        result = app.handler(_event(severity="medium", blast="contained", confidence=55), MagicMock())
        self.assertEqual(result["recommendation"], "open_pr")

    def test_no_action_always_routes_to_non_mutating_escalation(self):
        result = app.handler(
            _event(severity="low", blast="isolated", action="no_action", confidence=100),
            MagicMock(),
        )
        self.assertEqual(result["recommendation"], "escalate")
        self.assertIn("no_action_requested", result["risk_factors"])


if __name__ == "__main__":
    unittest.main()
