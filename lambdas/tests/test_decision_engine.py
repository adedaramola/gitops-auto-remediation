"""Unit tests for decision_engine/app.py"""
import base64
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

# yaml (PyYAML) is a real installed dependency. Explicitly ensure the real module is
# in sys.modules so that any stub placed by an earlier test file is overridden.
import yaml as _real_yaml
sys.modules["yaml"] = _real_yaml

os.environ.setdefault("GITHUB_OWNER", "test-org")
os.environ.setdefault("GITHUB_REPO", "test-repo")
os.environ.setdefault("GITHUB_APP_TOKEN_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:123:secret:gh")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import importlib
import decision_engine.app as app

importlib.reload(app)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

KUSTOMIZE_WITH_REPLICAS = """\
patches:
  - patch: |-
      - op: replace
        path: /spec/replicas
        value: 2
    target:
      kind: Deployment
"""

KUSTOMIZE_WITHOUT_REPLICAS = """\
namePrefix: staging-
"""

DEPLOYMENT_YAML = """\
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      containers:
        - name: app
          image: ghcr.io/org/app:v1.2.3
"""


# ---------------------------------------------------------------------------
# Pure function tests
# ---------------------------------------------------------------------------

class TestPatchReplicasKustomize(unittest.TestCase):
    def test_replaces_value_line(self):
        result = app._patch_replicas_kustomize(KUSTOMIZE_WITH_REPLICAS, 5)
        self.assertIn("value: 5", result)
        self.assertNotIn("value: 2", result)

    def test_only_first_match_replaced(self):
        yaml_two_patches = """\
patches:
  - patch: |-
      - op: replace
        path: /spec/replicas
        value: 2
    target:
      kind: Deployment
  - patch: |-
      - op: replace
        path: /spec/template/spec/containers/0/resources/limits/memory
        value: 99
    target:
      kind: Deployment
"""
        result = app._patch_replicas_kustomize(yaml_two_patches, 3)
        self.assertEqual(result.count("value: 3"), 1)
        self.assertIn("value: 99", result)

    def test_raises_when_no_value_line(self):
        with self.assertRaises(ValueError):
            app._patch_replicas_kustomize(KUSTOMIZE_WITHOUT_REPLICAS, 3)

    def test_ends_with_newline(self):
        result = app._patch_replicas_kustomize(KUSTOMIZE_WITH_REPLICAS, 2)
        self.assertTrue(result.endswith("\n"))


class TestPatchImageDeployment(unittest.TestCase):
    def test_replaces_tag(self):
        result = app._patch_image_deployment(DEPLOYMENT_YAML, "v2.0.0")
        self.assertIn("image: ghcr.io/org/app:v2.0.0", result)
        self.assertNotIn(":v1.2.3", result)

    def test_no_change_when_no_image(self):
        yaml_no_image = "kind: Service\n"
        result = app._patch_image_deployment(yaml_no_image, "v2.0.0")
        self.assertNotIn("v2.0.0", result)


class TestChooseActionHeuristic(unittest.TestCase):
    def _allowed(self, *actions):
        return {"allowed_actions": [{"action": a} for a in actions]}

    def test_prefers_rollback_on_5xx_data(self):
        bundle = {
            "env": "staging",
            "prometheus": {"error_rate_5xx": {"data": {"result": [{"value": [0, "0.5"]}]}}},
        }
        allowed = self._allowed("rollback_image", "scale_replicas")
        plan = app._choose_action_heuristic(bundle, allowed)
        self.assertEqual(plan["action"], "rollback_image")

    def test_falls_back_to_scale(self):
        bundle = {"env": "staging", "prometheus": {}}
        allowed = self._allowed("scale_replicas")
        plan = app._choose_action_heuristic(bundle, allowed)
        self.assertEqual(plan["action"], "scale_replicas")

    def test_falls_back_to_restart(self):
        bundle = {"env": "staging", "prometheus": {}}
        plan = app._choose_action_heuristic(bundle, {"allowed_actions": []})
        self.assertEqual(plan["action"], "restart_rollout")


class TestLlmPlanFallback(unittest.TestCase):
    """LLM plan should fall back to heuristic on any exception."""

    def _bundle(self):
        return {"incident_id": "inc-1", "env": "staging", "service": "svc", "prometheus": {}}

    def _allowed(self):
        return {"allowed_actions": [{"action": "scale_replicas"}, {"action": "rollback_image"}]}

    def test_falls_back_when_bedrock_raises(self):
        app.MODEL_PROVIDER = "bedrock"
        app.bedrock.invoke_model = MagicMock(side_effect=Exception("throttled"))
        plan = app._llm_plan(self._bundle(), self._allowed())
        self.assertIn(plan["action"], ["scale_replicas", "rollback_image", "restart_rollout"])
        self.assertEqual(plan["rationale"], "Fallback heuristic plan.")

    def test_falls_back_on_disallowed_action(self):
        app.MODEL_PROVIDER = "bedrock"
        mock_resp = MagicMock()
        mock_resp["body"].read.return_value = json.dumps({
            "content": [{"text": json.dumps({
                "action": "delete_cluster",  # not in allowed list
                "target": {"service": "svc", "env": "staging"},
                "params": {},
                "risk": "high",
                "rationale": "nope",
            })}]
        }).encode()
        app.bedrock.invoke_model = MagicMock(return_value=mock_resp)
        plan = app._llm_plan(self._bundle(), self._allowed())
        self.assertNotEqual(plan["action"], "delete_cluster")

    def test_markdown_fenced_llm_plan_is_used(self):
        app.MODEL_PROVIDER = "bedrock"
        fenced = "```json\n" + json.dumps({
            "action": "scale_replicas",
            "target": {"service": "svc", "env": "staging"},
            "params": {"replicas": 4},
            "risk": "low",
            "rationale": "Scale out to absorb load.",
        }, indent=2) + "\n```"
        mock_resp = MagicMock()
        mock_resp["body"].read.return_value = json.dumps({
            "content": [{"text": fenced}]
        }).encode()
        app.bedrock.invoke_model = MagicMock(return_value=mock_resp)
        plan = app._llm_plan(self._bundle(), self._allowed())
        self.assertEqual(plan["action"], "scale_replicas")
        self.assertEqual(plan["rationale"], "Scale out to absorb load.")


class TestPatchImageEdgeCases(unittest.TestCase):
    """Edge cases specific to the YAML-based _patch_image_deployment."""

    def test_registry_with_port_preserves_base(self):
        yaml_with_port = """\
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      containers:
        - name: app
          image: registry.example.com:5000/org/app:v1.0.0
"""
        result = app._patch_image_deployment(yaml_with_port, "v2.0.0")
        self.assertIn("registry.example.com:5000/org/app:v2.0.0", result)
        self.assertNotIn(":v1.0.0", result)

    def test_no_image_field_returns_valid_yaml(self):
        yaml_no_image = "kind: Service\nmetadata:\n  name: svc\n"
        result = app._patch_image_deployment(yaml_no_image, "v2.0.0")
        import yaml as _yaml
        doc = _yaml.safe_load(result)
        self.assertEqual(doc["kind"], "Service")

    def test_no_image_field_unchanged(self):
        yaml_no_image = "kind: Service\n"
        result = app._patch_image_deployment(yaml_no_image, "v2.0.0")
        self.assertNotIn("v2.0.0", result)


class TestPatchReplicasEdgeCases(unittest.TestCase):
    """Edge cases specific to the YAML-based _patch_replicas_kustomize."""

    _KUSTOMIZE_MULTI_PATCH = """\
patches:
  - patch: |-
      - op: replace
        path: /spec/replicas
        value: 2
    target:
      kind: Deployment
  - patch: |-
      - op: replace
        path: /spec/template/spec/containers/0/resources/limits/memory
        value: 512Mi
    target:
      kind: Deployment
"""

    def test_only_replicas_patch_is_updated(self):
        result = app._patch_replicas_kustomize(self._KUSTOMIZE_MULTI_PATCH, 7)
        self.assertIn("value: 7", result)
        self.assertIn("512Mi", result)

    def test_raises_when_no_replicas_op(self):
        kustomize_no_replicas = """\
patches:
  - patch: |-
      - op: replace
        path: /metadata/labels/version
        value: v2
    target:
      kind: Deployment
"""
        with self.assertRaises(ValueError):
            app._patch_replicas_kustomize(kustomize_no_replicas, 5)

    def test_output_is_valid_yaml(self):
        import yaml as _yaml
        result = app._patch_replicas_kustomize(KUSTOMIZE_WITH_REPLICAS, 4)
        _yaml.safe_load(result)


# ---------------------------------------------------------------------------
# Handler-level tests
# ---------------------------------------------------------------------------

def _make_s3_body(bundle: dict):
    body = MagicMock()
    body.read.return_value = json.dumps(bundle).encode("utf-8")
    return {"Body": body}


def _kustomize_file_obj():
    raw = KUSTOMIZE_WITH_REPLICAS.encode()
    return {"content": base64.b64encode(raw).decode(), "sha": "file-sha-123"}


def _deployment_file_obj():
    raw = DEPLOYMENT_YAML.encode()
    return {"content": base64.b64encode(raw).decode(), "sha": "deploy-sha-456"}


class TestHandlerScaleReplicas(unittest.TestCase):
    """Handler opens a PR for scale_replicas using dynamic service/env path."""

    def _run(self, service="payments", env="staging"):
        bundle = {"incident_id": "inc-1234-abcd", "service": service,
                  "env": env, "prometheus": {}}
        plan = {"action": "scale_replicas",
                "target": {"service": service, "env": env},
                "params": {"replicas": 3}, "risk": "low", "rationale": "test"}
        app.s3.get_object.return_value = _make_s3_body(bundle)
        with (
            patch.object(app, "_get_github_token", return_value="tok"),
            patch.object(app, "_fetch_allowed_actions", return_value={}),
            patch.object(app, "_llm_plan", return_value=plan),
            patch.object(app, "_find_existing_pr", return_value=None),
            patch.object(app, "_get_ref_sha", return_value="base-sha"),
            patch.object(app, "_create_branch", return_value={}),
            patch.object(app, "_get_file", return_value=_kustomize_file_obj()),
            patch.object(app, "_put_file", return_value={}),
            patch.object(app, "_open_pr", return_value={"number": 7, "html_url": "https://github.com/pr/7"}),
            patch.object(app, "_emit"),
            patch.object(app, "_audit_write"),
        ):
            event = {"detail": {"s3_bucket": "test-bucket", "s3_key": "incidents/inc-1234-abcd.json"}}
            return app.handler(event, MagicMock())

    def test_returns_200(self):
        resp = self._run()
        self.assertEqual(resp["statusCode"], 200)

    def test_action_in_response(self):
        body = json.loads(self._run()["body"])
        self.assertEqual(body["action"], "scale_replicas")

    def test_pr_number_in_response(self):
        body = json.loads(self._run()["body"])
        self.assertEqual(body["pr_number"], 7)

    def test_gitops_path_uses_service_and_env(self):
        body = json.loads(self._run(service="checkout", env="prod")["body"])
        self.assertIn("gitops/apps/checkout/overlays/prod/kustomization.yaml",
                      body["changed_files"])

    def test_no_hardcoded_demo_service_in_path(self):
        body = json.loads(self._run(service="checkout")["body"])
        self.assertNotIn("demo-service", body["changed_files"][0])


class TestHandlerIdempotency(unittest.TestCase):
    """Handler returns early if a PR already exists for this branch."""

    def test_existing_pr_returns_early(self):
        bundle = {"incident_id": "inc-1234-abcd", "service": "svc",
                  "env": "staging", "prometheus": {}}
        existing_pr = {"number": 3, "html_url": "https://github.com/pr/3"}
        app.s3.get_object.return_value = _make_s3_body(bundle)
        with (
            patch.object(app, "_get_github_token", return_value="tok"),
            patch.object(app, "_fetch_allowed_actions", return_value={}),
            patch.object(app, "_llm_plan", return_value={
                "action": "scale_replicas",
                "target": {"service": "svc", "env": "staging"},
                "params": {}, "risk": "low", "rationale": "x"}),
            patch.object(app, "_find_existing_pr", return_value=existing_pr),
        ):
            event = {"detail": {"s3_bucket": "b", "s3_key": "k"}}
            resp = app.handler(event, MagicMock())

        body = json.loads(resp["body"])
        self.assertEqual(body["message"], "PR already exists")
        self.assertEqual(body["pr_number"], 3)


class TestHandlerRollbackImage(unittest.TestCase):
    """Handler writes deployment.yaml for rollback_image using dynamic path."""

    def test_rollback_uses_service_path(self):
        bundle = {"incident_id": "inc-rollback", "service": "auth",
                  "env": "prod", "prometheus": {}}
        plan = {"action": "rollback_image",
                "target": {"service": "auth", "env": "prod"},
                "params": {"tag": "v1.9.0"}, "risk": "medium", "rationale": "bad deploy"}
        app.s3.get_object.return_value = _make_s3_body(bundle)
        with (
            patch.object(app, "_get_github_token", return_value="tok"),
            patch.object(app, "_fetch_allowed_actions", return_value={}),
            patch.object(app, "_llm_plan", return_value=plan),
            patch.object(app, "_find_existing_pr", return_value=None),
            patch.object(app, "_get_ref_sha", return_value="sha"),
            patch.object(app, "_create_branch", return_value={}),
            patch.object(app, "_get_file", return_value=_deployment_file_obj()),
            patch.object(app, "_put_file", return_value={}),
            patch.object(app, "_open_pr", return_value={"number": 9, "html_url": "https://github.com/pr/9"}),
            patch.object(app, "_emit"),
            patch.object(app, "_audit_write"),
        ):
            event = {"detail": {"s3_bucket": "b", "s3_key": "k"}}
            resp = app.handler(event, MagicMock())

        body = json.loads(resp["body"])
        self.assertEqual(body["action"], "rollback_image")
        self.assertIn("gitops/apps/auth/base/deployment.yaml", body["changed_files"])


class TestAuditWrite(unittest.TestCase):
    def test_skips_when_no_table(self):
        original = app.AUDIT_TABLE_NAME
        app.AUDIT_TABLE_NAME = ""
        app._audit_write("inc-1", {"action": "scale_replicas"})  # must not raise
        app.AUDIT_TABLE_NAME = original

    def test_writes_to_dynamodb_when_table_set(self):
        app.AUDIT_TABLE_NAME = "test-audit-table"
        app.dynamodb.put_item = MagicMock()
        app._audit_write("inc-1", {"action": "scale_replicas", "service": "payments"})
        app.dynamodb.put_item.assert_called_once()
        call_kwargs = app.dynamodb.put_item.call_args[1]
        self.assertEqual(call_kwargs["TableName"], "test-audit-table")
        app.AUDIT_TABLE_NAME = ""


class TestEventEmission(unittest.TestCase):
    def test_handler_accepts_raw_step_functions_state_without_emitting_action_dispatched(self):
        bundle = {"incident_id": "inc-stepfn", "service": "demo-service", "env": "staging", "prometheus": {}}
        plan = {
            "action": "scale_replicas",
            "target": {"service": "demo-service", "env": "staging"},
            "params": {"replicas": 3},
            "risk": "low",
            "rationale": "test",
        }
        app.s3.get_object.return_value = _make_s3_body(bundle)
        with (
            patch.object(app, "_get_github_token", return_value="tok"),
            patch.object(app, "_fetch_allowed_actions", return_value={}),
            patch.object(app, "_llm_plan", return_value=plan),
            patch.object(app, "_find_existing_pr", return_value=None),
            patch.object(app, "_get_ref_sha", return_value="base-sha"),
            patch.object(app, "_create_branch", return_value={}),
            patch.object(app, "_get_file", return_value=_kustomize_file_obj()),
            patch.object(app, "_put_file", return_value={}),
            patch.object(app, "_open_pr", return_value={"number": 11, "html_url": "https://github.com/pr/11"}),
            patch.object(app, "_emit") as mock_emit,
            patch.object(app, "_audit_write"),
        ):
            resp = app.handler({"s3_bucket": "b", "s3_key": "k"}, MagicMock())

        self.assertEqual(resp["statusCode"], 200)
        mock_emit.assert_not_called()

    def _auto_apply_event(self):
        return {"s3_bucket": "b", "s3_key": "k", "risk": {"recommendation": "auto_apply"}}

    def _plan(self):
        return {
            "action": "scale_replicas",
            "target": {"service": "demo-service", "env": "staging"},
            "params": {"replicas": 3},
            "risk": "low",
            "rationale": "test",
        }

    def _bundle(self):
        return {"incident_id": "inc-auto", "service": "demo-service", "env": "staging", "prometheus": {}}

    def test_auto_apply_route_merges_pr_and_emits_action_dispatched(self):
        app.s3.get_object.return_value = _make_s3_body(self._bundle())
        with (
            patch.object(app, "_get_github_token", return_value="tok"),
            patch.object(app, "_fetch_allowed_actions", return_value={}),
            patch.object(app, "_llm_plan", return_value=self._plan()),
            patch.object(app, "_find_existing_pr", return_value=None),
            patch.object(app, "_get_ref_sha", return_value="base-sha"),
            patch.object(app, "_create_branch", return_value={}),
            patch.object(app, "_get_file", return_value=_kustomize_file_obj()),
            patch.object(app, "_put_file", return_value={}),
            patch.object(app, "_open_pr", return_value={"number": 21, "html_url": "https://github.com/pr/21"}),
            patch.object(app, "_merge_pr", return_value={"merged": True}) as mock_merge,
            patch.object(app, "_emit") as mock_emit,
            patch.object(app, "_audit_write") as mock_audit,
        ):
            resp = app.handler(self._auto_apply_event(), MagicMock())

        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        self.assertEqual(body["message"], "PR auto-applied")
        mock_merge.assert_called_once()
        self.assertIn("auto-applied by decision engine",
                      mock_merge.call_args.kwargs.get("commit_message", ""))
        detail_type, detail = mock_emit.call_args[0]
        self.assertEqual(detail_type, "ActionDispatched")
        self.assertEqual(detail["incident_id"], "inc-auto")
        self.assertEqual(detail["pr_number"], 21)
        self.assertEqual(detail["route"], "auto_apply")
        audit_record = mock_audit.call_args[0][1]
        self.assertEqual(audit_record["outcome"], "auto_applied")

    def test_auto_apply_merge_failure_leaves_pr_open_without_emitting(self):
        app.s3.get_object.return_value = _make_s3_body(self._bundle())
        with (
            patch.object(app, "_get_github_token", return_value="tok"),
            patch.object(app, "_fetch_allowed_actions", return_value={}),
            patch.object(app, "_llm_plan", return_value=self._plan()),
            patch.object(app, "_find_existing_pr", return_value=None),
            patch.object(app, "_get_ref_sha", return_value="base-sha"),
            patch.object(app, "_create_branch", return_value={}),
            patch.object(app, "_get_file", return_value=_kustomize_file_obj()),
            patch.object(app, "_put_file", return_value={}),
            patch.object(app, "_open_pr", return_value={"number": 22, "html_url": "https://github.com/pr/22"}),
            patch.object(app, "_merge_pr", side_effect=RuntimeError("GitHub API error 405: checks pending")),
            patch.object(app, "_emit") as mock_emit,
            patch.object(app, "_audit_write") as mock_audit,
        ):
            resp = app.handler(self._auto_apply_event(), MagicMock())

        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        self.assertEqual(body["message"], "PR opened")
        mock_emit.assert_not_called()
        audit_record = mock_audit.call_args[0][1]
        self.assertEqual(audit_record["outcome"], "pending")

    def test_auto_apply_retry_merges_existing_pr(self):
        app.s3.get_object.return_value = _make_s3_body(self._bundle())
        with (
            patch.object(app, "_get_github_token", return_value="tok"),
            patch.object(app, "_fetch_allowed_actions", return_value={}),
            patch.object(app, "_llm_plan", return_value=self._plan()),
            patch.object(app, "_find_existing_pr",
                         return_value={"number": 23, "html_url": "https://github.com/pr/23"}),
            patch.object(app, "_merge_pr", return_value={"merged": True}) as mock_merge,
            patch.object(app, "_emit") as mock_emit,
            patch.object(app, "_audit_write"),
        ):
            resp = app.handler(self._auto_apply_event(), MagicMock())

        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        self.assertEqual(body["message"], "PR auto-applied")
        mock_merge.assert_called_once()
        mock_emit.assert_called_once()


class TestAutoApplyGuardrails(unittest.TestCase):
    def setUp(self):
        app._kill_switch_cache.update({"value": None, "expires_at": 0.0})

    def tearDown(self):
        app.AUTO_APPLY_ENABLED_PARAM = ""
        app.AUDIT_TABLE_NAME = ""
        app._kill_switch_cache.update({"value": None, "expires_at": 0.0})

    def test_kill_switch_unset_param_allows(self):
        app.AUTO_APPLY_ENABLED_PARAM = ""
        self.assertTrue(app._auto_apply_enabled())

    def test_kill_switch_false_blocks(self):
        app.AUTO_APPLY_ENABLED_PARAM = "/test/auto-apply-enabled"
        app.ssm.get_parameter = MagicMock(return_value={"Parameter": {"Value": "false"}})
        self.assertFalse(app._auto_apply_enabled())

    def test_kill_switch_true_allows_and_caches(self):
        app.AUTO_APPLY_ENABLED_PARAM = "/test/auto-apply-enabled"
        app.ssm.get_parameter = MagicMock(return_value={"Parameter": {"Value": "true"}})
        self.assertTrue(app._auto_apply_enabled())
        self.assertTrue(app._auto_apply_enabled())
        app.ssm.get_parameter.assert_called_once()  # second call served from cache

    def test_kill_switch_read_failure_fails_closed(self):
        app.AUTO_APPLY_ENABLED_PARAM = "/test/auto-apply-enabled"
        app.ssm.get_parameter = MagicMock(side_effect=Exception("ssm down"))
        self.assertFalse(app._auto_apply_enabled())

    def test_rate_limit_under_budget_allows(self):
        app.AUDIT_TABLE_NAME = "audit"
        app.dynamodb.update_item = MagicMock(
            return_value={"Attributes": {"applied": {"N": str(app.AUTO_APPLY_MAX_PER_HOUR)}}})
        self.assertTrue(app._auto_apply_rate_ok())

    def test_rate_limit_over_budget_blocks(self):
        app.AUDIT_TABLE_NAME = "audit"
        app.dynamodb.update_item = MagicMock(
            return_value={"Attributes": {"applied": {"N": str(app.AUTO_APPLY_MAX_PER_HOUR + 1)}}})
        self.assertFalse(app._auto_apply_rate_ok())

    def test_rate_limit_counter_failure_fails_closed(self):
        app.AUDIT_TABLE_NAME = "audit"
        app.dynamodb.update_item = MagicMock(side_effect=Exception("dynamo down"))
        self.assertFalse(app._auto_apply_rate_ok())

    def test_kill_switch_blocks_merge_leaving_pr_open(self):
        bundle = {"incident_id": "inc-gated", "service": "demo-service", "env": "staging", "prometheus": {}}
        plan = {
            "action": "scale_replicas",
            "target": {"service": "demo-service", "env": "staging"},
            "params": {"replicas": 3},
            "risk": "low",
            "rationale": "test",
        }
        app.s3.get_object.return_value = _make_s3_body(bundle)
        with (
            patch.object(app, "_get_github_token", return_value="tok"),
            patch.object(app, "_fetch_allowed_actions", return_value={}),
            patch.object(app, "_llm_plan", return_value=plan),
            patch.object(app, "_find_existing_pr", return_value=None),
            patch.object(app, "_get_ref_sha", return_value="base-sha"),
            patch.object(app, "_create_branch", return_value={}),
            patch.object(app, "_get_file", return_value=_kustomize_file_obj()),
            patch.object(app, "_put_file", return_value={}),
            patch.object(app, "_open_pr", return_value={"number": 31, "html_url": "https://github.com/pr/31"}),
            patch.object(app, "_auto_apply_enabled", return_value=False),
            patch.object(app, "_merge_pr") as mock_merge,
            patch.object(app, "_emit") as mock_emit,
            patch.object(app, "_audit_write") as mock_audit,
        ):
            event = {"s3_bucket": "b", "s3_key": "k", "risk": {"recommendation": "auto_apply"}}
            resp = app.handler(event, MagicMock())

        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(json.loads(resp["body"])["message"], "PR opened")
        mock_merge.assert_not_called()
        mock_emit.assert_not_called()
        self.assertEqual(mock_audit.call_args[0][1]["outcome"], "pending")


if __name__ == "__main__":
    unittest.main()
