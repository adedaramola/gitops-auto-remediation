# Deploying GitOps Auto-Remediation

## Prerequisites
- Terraform >= 1.10
- AWS credentials configured
- GitHub repo created (this repo)
- A GitHub token stored in Secrets Manager as JSON: `{ "token": "..." }`

## Steps

1. Review `terraform/backend.tf`.
   The repo now defaults to an S3 remote backend with native lockfile-based locking. Create the bucket it references, or update the backend block to a bucket/key you control before you run `terraform init`.

2. Create `terraform/terraform.tfvars` from `terraform/terraform.tfvars.example`:
   ```hcl
   github_owner            = "your-org"
   github_repo             = "your-gitops-repo"
   gitops_repo_revision    = "main"
   github_token_secret_arn = "arn:aws:secretsmanager:..."
   bootstrap_argocd_applications = true
   enable_multi_agent      = false
   model_provider          = "bedrock"
   bedrock_model_id        = "anthropic.claude-3-haiku-20240307-v1:0"
   enable_private_prometheus_endpoint = true
   enable_k8s_readonly_enrichment = true
   alarm_email             = "oncall@example.com"
   monthly_budget_usd      = 200
   auto_apply_max_per_hour = 3
   ```

   This is the safest repeatable MVP demo profile. Switch `enable_multi_agent` to `true` only after the model-access preflight passes.

3. Deploy infrastructure:
   ```bash
   cd terraform
   terraform init -reconfigure
   terraform apply -var-file=terraform.tfvars
   ```

4. Terraform bootstraps the Argo CD Applications for `demo-staging`, `demo-prod`, and `platform-policies` when `bootstrap_argocd_applications = true`. No manual `kubectl apply` is required for the normal demo path.

5. Record these outputs after apply:
   - `webhook_url`
   - `alarms_topic_arn`
   - `auto_apply_kill_switch`

6. Configure Alertmanager webhook using the `webhook_url` Terraform output.
   See `docs/alertmanager-webhook.md`.

7. Run the demo preflight:
   ```bash
   make demo-preflight
   ```

8. Trigger an alert and verify:
   - S3 signal bundle is created under `incidents/`
   - EventBridge emits `SignalBundled` or `SentinelPipelineTriggered`
   - Step Functions execution is created when multi-agent mode is enabled
   - GitHub PR is opened by the Decision Engine, and auto-merged only when the `auto_apply` guardrails allow it
   - Outcome Validator posts `OutcomeValidated` or `OutcomeFailed`
   - Alarm notifications reach the SNS topic, and the optional email subscription if configured

## Recommended MVP demo path
Use a single polished scenario instead of trying to prove every capability:

1. Deploy `demo-service`
2. Trigger `HighHTTP5xxErrorRate`
3. Show the enriched signal bundle in S3
4. Show the Step Functions execution and route decision
5. Show the GitHub PR in the GitOps repo
6. Merge the PR, or show the auto-merge guardrails allowing it, and then show Argo CD reconciliation
7. Show the Outcome Validator result

## Optional: Prometheus query URL
Set `prometheus_query_url` in `terraform.tfvars` if you have a reachable Prometheus endpoint.

## GitHub Actions → AWS (ActionDispatched trigger)
To emit `ActionDispatched` events on PR merge, set these GitHub repo secrets:
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_REGION`
- `AWS_EVENT_BUS_NAME`

The workflow `notify-action-dispatched.yaml` extracts the incident ID from the merge commit and puts an EventBridge event that triggers the Outcome Validator.

## Auto-apply guardrails
High-confidence routes can merge immediately, but only when both controls pass:
- `auto_apply_kill_switch` SSM parameter is still `true`
- the hourly `auto_apply_max_per_hour` budget has not been exhausted

If either guardrail blocks the merge, the PR stays open for human review.

## Apply GitOps policies
OPA Gatekeeper constraints and the `allowed-actions.yaml` contract live under `gitops/policies/`.
Both staging and prod cluster kustomizations include `../../policies`.
