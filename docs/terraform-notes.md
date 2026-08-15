# Terraform Notes

The current Terraform layout provisions the full demo stack and a few newer operational controls that older notes did not cover.

## What the root module now owns
- VPC + EKS via the registry modules
- Argo CD, kube-prometheus-stack, and Gatekeeper via Helm/Kubernetes providers
- API Gateway webhook intake, EventBridge routing, Step Functions, and all 7 Lambdas
- DynamoDB tables for incident dedup and audit logging
- SNS alarms topic plus CloudWatch alarms for Lambda runtime failures, Step Functions failures/timeouts, and EventBridge DLQ depth
- Monthly AWS budget notifications wired to the same SNS alarms topic
- Auto-apply guardrails: SSM kill switch and hourly merge budget

## Backend
Terraform now expects an S3 remote backend with `use_lockfile = true` in [terraform/backend.tf](/Users/adedaramola/IT-Practice/AI-Projects/portfolio/gitops-auto-remediation/terraform/backend.tf:1). Create or replace that bucket configuration before `terraform init` if you are deploying this in a different AWS account.

## GitHub token secret format
Store JSON in Secrets Manager:
```json
{ "token": "..." }
```

That token can be a classic PAT for the MVP or an installation token issued by a GitHub App. The runtime just needs a bearer token in the `token` field.

## Important
- Keep branch protection and required checks enabled before relying on auto-merge.
- If you rotate the GitHub token, remember the Lambda caches it for up to 5 minutes on warm invocations.
