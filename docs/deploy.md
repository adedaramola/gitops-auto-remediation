# Deploying GitOps Auto-Remediation

## Prerequisites
- Terraform >= 1.6
- AWS credentials configured
- GitHub repo created (this repo)
- A GitHub token stored in Secrets Manager as JSON: `{ "token": "ghp_..." }`

## Steps

1. Create `terraform/terraform.tfvars` from `terraform/terraform.tfvars.example`:
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
   ```

   This is the safest repeatable MVP demo profile. Switch `enable_multi_agent` to `true` only after the model-access preflight passes.

2. Deploy infrastructure:
   ```bash
   cd terraform
   terraform init
   terraform apply -var-file=terraform.tfvars
   ```

3. Terraform bootstraps the Argo CD Applications for `demo-staging`, `demo-prod`, and `platform-policies` when `bootstrap_argocd_applications = true`. No manual `kubectl apply` is required for the normal demo path.

4. Configure Alertmanager webhook using the `webhook_url` Terraform output.
   See `docs/alertmanager-webhook.md`.

5. Run the demo preflight:
   ```bash
   make demo-preflight
   ```

6. Trigger an alert and verify:
   - S3 signal bundle is created under `incidents/`
   - Step Functions execution is created
   - GitHub PR is opened by the Decision Engine or Action Planner
   - Outcome Validator posts `OutcomeValidated` or `OutcomeFailed`

## Recommended MVP demo path
Use a single polished scenario instead of trying to prove every capability:

1. Deploy `demo-service`
2. Trigger `HighHTTP5xxErrorRate`
3. Show the enriched signal bundle in S3
4. Show the Step Functions execution and route decision
5. Show the GitHub PR in the GitOps repo
6. Merge the PR and show Argo CD reconciliation
7. Show the Outcome Validator result

## Optional: Prometheus query URL
Set `prometheus_query_url` in `terraform.tfvars` if you have a reachable Prometheus endpoint.

## GitHub Actions → AWS (ActionDispatched trigger)
To emit `ActionDispatched` events on PR merge, set these GitHub repo secrets:
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_REGION`

The workflow `notify-action-dispatched.yaml` extracts the incident ID from the merge commit and puts an EventBridge event that triggers the Outcome Validator.

## Apply GitOps policies
OPA Gatekeeper constraints and the `allowed-actions.yaml` contract live under `gitops/policies/`.
Both staging and prod cluster kustomizations include `../../policies`.
