# Deploying GitOps Sentinel

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
   github_token_secret_arn = "arn:aws:secretsmanager:..."
   enable_multi_agent      = true
   model_provider          = "bedrock"
   bedrock_model_id        = "anthropic.claude-3-haiku-20240307-v1:0"
   enable_private_prometheus_endpoint = true
   enable_k8s_readonly_enrichment = true
   ```

   This is the recommended MVP demo profile. It gives you the full confidence-gated pipeline plus Prometheus-backed outcome validation.

2. Deploy infrastructure:
   ```bash
   cd terraform
   terraform init
   terraform apply -var-file=terraform.tfvars
   ```

3. Install Argo CD Applications — update `repoURL` in:
   - `gitops/argocd/application-staging.yaml`
   - `gitops/argocd/application-prod.yaml`

   Then apply:
   ```bash
   kubectl apply -n argocd -f gitops/argocd/application-staging.yaml
   kubectl apply -n argocd -f gitops/argocd/application-prod.yaml
   ```

4. Configure Alertmanager webhook using the `webhook_url` Terraform output.
   See `docs/alertmanager-webhook.md`.

5. Trigger an alert and verify:
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
