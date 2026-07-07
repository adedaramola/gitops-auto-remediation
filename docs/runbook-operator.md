# GitOps Auto-Remediation — Operator Runbook

## Deploy
```bash
cd terraform && terraform init && terraform apply -var-file=terraform.tfvars
```

Record outputs:
- `webhook_url` — configure in Alertmanager receiver
- `signals_bucket_name` — S3 bucket for signal bundles
- `event_bus_name` — EventBridge custom bus
- `signals_table_name` — DynamoDB dedup table

## Configure Argo CD Applications
Set `bootstrap_argocd_applications = true` in `terraform/terraform.tfvars`. Terraform bootstraps `demo-staging`, `demo-prod`, and `platform-policies` for the normal demo path, so no manual `kubectl apply` is required.

Verify the apps are healthy before the demo:

```bash
make demo-preflight
```

## Configure Alertmanager webhook
See `docs/alertmanager-webhook.md` and set:
- receiver webhook URL = Terraform `webhook_url` output

## Validate the full loop
1. Trigger a test alert (or lower thresholds temporarily)
2. Confirm:
   - Signal Collector writes `s3://<bucket>/incidents/inc-*.json`
   - Decision Engine (or the multi-agent pipeline when enabled) opens a GitHub PR
   - CI passes on the PR
   - Merge PR → GitHub Actions emits `ActionDispatched` → Argo CD syncs cluster
   - Outcome Validator queries Prometheus and emits `OutcomeValidated` or `OutcomeFailed`
   - DynamoDB Audit Log has entries for `action_dispatched` and `outcome_validated`

## Auto-apply guardrails

The Decision Engine only auto-merges high-confidence PRs when two guardrails pass; otherwise the PR stays open for human review and an `AutoApplyBlocked` metric is emitted.

**Kill switch** — disable all auto-merging immediately (takes effect within ~30s):

```bash
aws ssm put-parameter --name /gitops-sentinel/auto-apply-enabled --value false --overwrite
# re-enable
aws ssm put-parameter --name /gitops-sentinel/auto-apply-enabled --value true --overwrite
```

Terraform ignores value drift on this parameter, so an operator flip survives `terraform apply`. If the parameter can't be read at all, auto-apply fails closed (disabled).

**Rate limit** — at most `auto_apply_max_per_hour` auto-merges per hour (default 3), tracked by an atomic counter in the audit table (`incident_id = rate#auto_apply`). Merges beyond the budget open PRs for review instead. Raise the limit in tfvars if legitimate remediations are being throttled.

**Branch protection** — `main` requires all seven CI checks (validate-gitops, check-lambda-sync, terraform-validate, tflint, security-scan, unit-tests, policy-check) to pass before merging. `enforce_admins` is off, so repo admins can still push directly to main. Note: if the Decision Engine's GitHub token belongs to a repo admin, its API merges bypass pending checks (auto-apply merges immediately); with a non-admin token the merge is rejected until checks pass and the PR falls back to human review.

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Decision Engine can't open PR | GitHub token secret wrong format or missing permissions | Check secret JSON: `{ "token": "ghp_..." }`, ensure `contents:write` + `pull_requests:write` |
| Outcome Validator cannot verify recovery and emits `OutcomeFailed` | `PROMETHEUS_QUERY_URL` not set or not reachable | Set `prometheus_query_url` in tfvars, confirm the endpoint is reachable, and redeploy |
| Gatekeeper rejects change | Action outside `allowed-actions.yaml` bounds | Expected — add action to the allowed list if intentional |
| Signal dedup suppressing alerts | DynamoDB TTL not expired (30-min window) | Wait for TTL or manually delete the dedup record |
| Step Functions pipeline stuck | Agent Lambda timeout | Check X-Ray trace for which state timed out; increase timeout or check Bedrock throttling |
