"""Integration tests for the multi-agent Step Functions pipeline.

Drives the real Lambda handlers in the exact sequence and envelope shapes the
state machine wires up (ResultPath per stage, Parameters {"detail.$": "$"} on
the PR tasks), with only the external boundaries mocked (S3, LLM, GitHub).
Catches cross-Lambda contract bugs — e.g. the raw-state-vs-EventBridge-envelope
mismatch — that per-function unit tests cannot see.
"""
import base64
import io
import json
import os
import pathlib
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub heavy dependencies before import (same pattern as the unit test files)
# ---------------------------------------------------------------------------

boto3_stub = types.ModuleType("boto3")
boto3_stub.client = MagicMock(return_value=MagicMock())
sys.modules.setdefault("boto3", boto3_stub)

requests_stub = types.ModuleType("requests")
requests_stub.request = MagicMock()
requests_stub.post = MagicMock()
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

import yaml as _real_yaml
sys.modules["yaml"] = _real_yaml

os.environ.setdefault("GITHUB_OWNER", "test-org")
os.environ.setdefault("GITHUB_REPO", "test-repo")
os.environ.setdefault("GITHUB_APP_TOKEN_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:123:secret:gh")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import classifier_agent.app as classifier
import root_cause_agent.app as root_cause
import action_planner.app as planner
import confidence_scorer.app as scorer
import decision_engine.app as decision

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SFN_DEFINITION = (REPO_ROOT / "terraform" / "modules" / "step_functions" / "main.tf").read_text()

BUNDLE = {
    "incident_id": "inc-e2e-0001",
    "service": "demo-service",
    "env": "staging",
    "alert": {"labels": {"alertname": "HighHTTP5xxErrorRate"}},
    "prometheus": {"error_rate": 0.42},
}

KUSTOMIZE_YAML = """\
patches:
  - patch: |-
      - op: replace
        path: /spec/replicas
        value: 2
    target:
      kind: Deployment
"""


def _s3_body():
    return {"Body": io.BytesIO(json.dumps(BUNDLE).encode("utf-8"))}


def _kustomize_file_obj():
    return {"content": base64.b64encode(KUSTOMIZE_YAML.encode()).decode(), "sha": "abc"}


def _agent_llm_json(severity, blast, confidence):
    """Canned LLM responses keyed by which schema the prompt asks for."""
    triage = json.dumps({
        "severity_class": severity,
        "incident_type": "HighErrorRate",
        "blast_radius": blast,
        "priority": 2,
        "key_signals": ["5xx spike"],
    })
    diagnosis = json.dumps({
        "root_cause": "Bad deploy exhausted replicas",
        "contributing_factors": ["traffic spike"],
        "affected_components": ["demo-service"],
        "diagnosis_confidence": confidence,
    })
    remediation = json.dumps({
        "action": "scale_replicas",
        "params": {"replicas": 3},
        "target": {"service": "demo-service", "env": "staging"},
        "reasoning": "Scale out to absorb load",
        "alternatives": [],
    })
    return triage, diagnosis, remediation


def run_pipeline(severity, blast, confidence):
    """Mirrors the state machine: each Task gets the full accumulated state and
    its result lands at the ASL-defined ResultPath; the PR tasks wrap the state
    as {"detail": state}. Returns (state, route, decision_response)."""
    triage_json, diagnosis_json, remediation_json = _agent_llm_json(severity, blast, confidence)

    # EventBridge target input_path "$.detail" of SentinelPipelineTriggered
    state = {
        "incident_id": BUNDLE["incident_id"],
        "s3_bucket": "incident-bucket",
        "s3_key": "incidents/inc-e2e-0001.json",
        "service": BUNDLE["service"],
        "env": BUNDLE["env"],
    }

    for module in (classifier, root_cause, planner):
        module.s3.get_object = MagicMock(side_effect=lambda **_: _s3_body())

    with (
        patch.object(classifier, "_call_llm", return_value=triage_json),
        patch.object(root_cause, "_call_llm", return_value=diagnosis_json),
        patch.object(planner, "_call_llm", return_value=remediation_json),
        patch.object(planner, "_fetch_allowed_actions", return_value={
            "allowed_actions": [{"action": "scale_replicas"}, {"action": "restart_rollout"}]}),
    ):
        state["triage"] = classifier.handler(dict(state), MagicMock())          # ResultPath $.triage
        state["diagnosis"] = root_cause.handler(dict(state), MagicMock())       # ResultPath $.diagnosis
        state["remediation"] = planner.handler(dict(state), MagicMock())        # ResultPath $.remediation
        state["risk"] = scorer.handler(dict(state), MagicMock())                # ResultPath $.risk

    route = state["risk"]["recommendation"]  # RouteByConfidence choice variable
    if route == "escalate":
        return state, route, None

    plan = {
        "action": state["remediation"]["action"],
        "target": state["remediation"]["target"],
        "params": state["remediation"]["params"],
        "risk": state["risk"]["risk_level"],
        "rationale": state["remediation"]["reasoning"],
    }
    decision.s3.get_object = MagicMock(side_effect=lambda **_: _s3_body())
    with (
        patch.object(decision, "_get_github_token", return_value="tok"),
        patch.object(decision, "_fetch_allowed_actions", return_value={}),
        patch.object(decision, "_llm_plan", return_value=plan),
        patch.object(decision, "_find_existing_pr", return_value=None),
        patch.object(decision, "_get_ref_sha", return_value="base-sha"),
        patch.object(decision, "_create_branch", return_value={}),
        patch.object(decision, "_get_file", return_value=_kustomize_file_obj()),
        patch.object(decision, "_put_file", return_value={}),
        patch.object(decision, "_open_pr",
                     return_value={"number": 42, "html_url": "https://github.com/pr/42"}),
        patch.object(decision, "_merge_pr", return_value={"merged": True}) as mock_merge,
        patch.object(decision, "_emit") as mock_emit,
        patch.object(decision, "_audit_write"),
    ):
        # QueueForAutoApply / OpenRemediationPR: Parameters {"detail.$": "$"}
        resp = decision.handler({"detail": state}, MagicMock())
        resp["_merge_called"] = mock_merge.called
        resp["_emit_called"] = mock_emit.called
    return state, route, resp


class TestStateMachineWiringContract(unittest.TestCase):
    """If the ASL wiring in terraform changes, these markers force this test
    file's simulation (run_pipeline) to be updated to match."""

    def test_result_paths_match_simulation(self):
        for marker in (
            'ResultPath = "$.triage"',
            'ResultPath = "$.diagnosis"',
            'ResultPath = "$.remediation"',
            'ResultPath = "$.risk"',
            '"$.risk.recommendation"',
        ):
            self.assertIn(marker, SFN_DEFINITION, f"state machine wiring changed: {marker}")

    def test_pr_tasks_wrap_state_in_detail_envelope(self):
        self.assertEqual(SFN_DEFINITION.count('"detail.$" = "$"'), 2,
                         "QueueForAutoApply and OpenRemediationPR must wrap state as detail")


class TestPipelineRouting(unittest.TestCase):
    def tearDown(self):
        decision.AUTO_APPLY_ENABLED_PARAM = ""
        decision.AUDIT_TABLE_NAME = ""
        decision._kill_switch_cache.update({"value": None, "expires_at": 0.0})

    def test_high_confidence_routes_to_auto_apply_and_merges(self):
        state, route, resp = run_pipeline(severity="low", blast="isolated", confidence=95)
        self.assertEqual(route, "auto_apply")
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(json.loads(resp["body"])["message"], "PR auto-applied")
        self.assertTrue(resp["_merge_called"])
        self.assertTrue(resp["_emit_called"])

    def test_medium_confidence_opens_pr_without_merging(self):
        state, route, resp = run_pipeline(severity="high", blast="contained", confidence=80)
        self.assertEqual(route, "open_pr")
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(json.loads(resp["body"])["message"], "PR opened")
        self.assertFalse(resp["_merge_called"])
        self.assertFalse(resp["_emit_called"])

    def test_low_confidence_escalates_without_pr(self):
        state, route, resp = run_pipeline(severity="critical", blast="broad", confidence=30)
        self.assertEqual(route, "escalate")
        self.assertIsNone(resp)

    def test_agent_contract_keys_flow_through_state(self):
        state, _, _ = run_pipeline(severity="low", blast="isolated", confidence=95)
        self.assertEqual(state["triage"]["severity_class"], "low")
        self.assertEqual(state["diagnosis"]["diagnosis_confidence"], 95)
        self.assertEqual(state["remediation"]["action"], "scale_replicas")
        self.assertIn(state["risk"]["recommendation"], ("auto_apply", "open_pr", "escalate"))


if __name__ == "__main__":
    unittest.main()
