# GitOps Auto-Remediation — Mid-Level Developer Implementation Guide

This guide assumes you are comfortable with AWS, Kubernetes, Python, and Terraform. Steps are concise — the focus is on architecture decisions, customisation points, and production considerations rather than hand-holding through CLI basics.

---

## System Design

### Signal Flow

```
Alertmanager
    │  POST /webhook (HMAC-signed)
    ▼
API Gateway (HTTP API)
    │
    ▼
Signal Collector Lambda
    ├── HMAC validation (X-Webhook-Secret header)
    ├── DynamoDB conditional write (dedup — 30-min TTL window)
    ├── Prometheus snapshot queries (optional enrichment)
    ├── EKS read-only k8s events (optional enrichment)
    ├── S3 PUT  →  incidents/<incident_id>.json
    └── EventBridge PUT
            ├── SignalBundled         →  Decision Engine Lambda (single-agent path)
            └── SentinelPipelineTriggered  →  Step Functions (multi-agent path)

Decision Engine Lambda (single-agent)
    ├── Reads signal bundle from S3
    ├── Fetches allowed-actions.yaml from GitHub
    ├── Calls Bedrock/OpenAI or falls back to heuristic
    ├── Checks for existing PR (idempotency)
    ├── Opens GitHub PR with patch
    └── Writes action_dispatched record to DynamoDB Audit Log

Step Functions — Auto-Remediation Pipeline (multi-agent)
    ├── ClassifierAgent   →  severity, blast radius, key signals
    ├── RootCauseAgent    →  LLM root cause + confidence score (0–100)
    ├── ActionPlanner     →  proposed action + alternatives
    ├── ConfidenceScorer  →  deterministic scoring + penalty model
    └── RouteByConfidence
            ├── ≥80 + low risk  →  auto_apply  (invokes the Decision Engine PR path)
            ├── 40–79           →  open_pr     (human review)
            └── <40             →  escalate    (PagerDuty / Slack)

GitHub Actions (on PR merge to main)
    └── notify-action-dispatched.yaml
            └── EventBridge PUT ActionDispatched

Outcome Validator Lambda
    ├── PromQL query  →  error rate < 20% threshold?
    ├── OutcomeValidated  →  success path
    └── OutcomeFailed     →  opens revert PR automatically
```

### Key Design Decisions

| Decision | Rationale |
|---|---|
| GitOps as execution engine | No agent ever writes directly to the cluster. All changes are auditable, reversible Git commits |
| Confidence-gated routing | System knows when it is certain enough to act autonomously vs when to ask a human |
| DynamoDB dedup | Prevents alert storms from triggering N identical remediations for the same incident |
| Heuristic fallback | If Bedrock is unavailable or returns an invalid response, a deterministic fallback runs — the system degrades gracefully |
| GitHub token cache | Token refreshed every 5 minutes via Secrets Manager — avoids per-invocation API calls |
| Separate IAM roles per Lambda | Least-privilege; each function only has the permissions it needs |

---

## Repository Layout

```
.
├── lambdas/
│   ├── signal_collector/app.py      # Webhook receiver, dedup, enrichment
│   ├── decision_engine/app.py       # LLM plan + PR creation
│   ├── outcome_validator/app.py     # PromQL check + revert PR
│   ├── classifier_agent/app.py      # Step Functions: incident classification
│   ├── root_cause_agent/app.py      # Step Functions: LLM root cause
│   ├── action_planner/app.py        # Step Functions: action proposal
│   ├── confidence_scorer/app.py     # Step Functions: deterministic scoring
│   ├── requirements.txt
│   └── tests/                       # Lambda unit tests with stub isolation
│
├── terraform/
│   ├── main.tf                      # Root module — wires everything together
│   ├── variables.tf
│   ├── outputs.tf
│   ├── terraform.tfvars.example
│   └── modules/
│       ├── lambda_signal_collector/
│       ├── lambda_decision_engine/
│       ├── lambda_outcome_validator/
│       ├── lambda_classifier_agent/
│       ├── lambda_root_cause_agent/
│       ├── lambda_action_planner/
│       ├── lambda_confidence_scorer/
│       ├── lambda_package/
│       ├── dynamodb_audit_log/
│       ├── dynamodb_incidents/
│       ├── eventbridge/
│       ├── eventbridge_rules/
│       ├── iam/                     # All Lambda + SFN roles in one module
│       ├── step_functions/
│       ├── apigw_webhook/
│       ├── s3_incidents/
│       ├── argocd/
│       ├── gatekeeper/
│       ├── log_shipping/
│       └── observability/
│
├── gitops/
│   ├── apps/demo-service/
│   │   ├── base/                    # deployment.yaml, service.yaml
│   │   └── overlays/{staging,prod}/ # kustomize patches
│   ├── clusters/{staging,prod}/     # top-level kustomizations
│   ├── policies/
│   │   ├── allowed-actions.yaml     # contract read by Decision Engine
│   │   └── gatekeeper/              # OPA ConstraintTemplate + Constraint
│   └── argocd/
│       ├── application-staging.yaml
│       └── application-prod.yaml
│
└── .github/workflows/
    ├── validate-pr.yaml             # kustomize build + pytest on PRs
    ├── policy-check.yaml            # enforce allowed-actions bounds
    └── notify-action-dispatched.yaml # fires ActionDispatched to EventBridge on merge
```

---

## Prerequisites

- AWS CLI v2, configured with sufficient IAM permissions
- Terraform >= 1.10
- kubectl >= 1.28, Helm >= 3.12
- Argo CD CLI (`brew install argocd`)
- GitHub CLI (`brew install gh`)
- Python 3.12
- Helm repos added:
  ```bash
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
  helm repo update
  ```

---

## Deployment

### 1. Fork the repo

Fork `adedaramola/gitops-auto-remediation` to your GitHub account. The Decision Engine writes PRs to this repo — it must be yours.

For the normal demo path, prefer Terraform Argo bootstrap over manual manifest edits. Set `github_owner`, `github_repo`, and `bootstrap_argocd_applications = true` in `terraform/terraform.tfvars`.

### 2. Create a GitHub token secret

For the MVP, a classic PAT with `repo` scope is fine. If you already have a GitHub App installation-token flow, that works too. The Lambda only requires a bearer token stored in Secrets Manager as `{ "token": "..." }`.

Example:

```bash
aws secretsmanager create-secret \
  --name "gitops-auto-remediation/github-token" \
  --secret-string '{"token":"ghp_YOUR_TOKEN"}' \
  --region us-east-1
```

Note the returned ARN.

### 3. Lambda packaging

No manual bundling step is required now. Terraform packages directly from `lambdas/<function>/` using the shared packaging flow in `terraform/modules/lambda_package/` and `terraform/scripts/build_lambda.sh`.

### 4. Create `terraform/terraform.tfvars`

```bash
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
```

Minimum required values:

```hcl
aws_region              = "us-east-1"
project_name            = "gitops-auto-remediation"
cluster_name            = "gitops-auto-remediation-cluster"
github_owner            = "YOUR_USERNAME"
github_repo             = "gitops-auto-remediation"
github_token_secret_arn = "arn:aws:secretsmanager:us-east-1:ACCOUNT:secret:gitops-auto-remediation/github-token-xxxxxx"
model_provider          = "bedrock"
vpc_cidr                = "10.20.0.0/16"
az_count                = 2
alarm_email             = "oncall@example.com"
monthly_budget_usd      = 200
auto_apply_max_per_hour = 3
webhook_secret          = "$(openssl rand -hex 32)"  # replace with actual value
enable_multi_agent      = false
```

Use `enable_multi_agent = false` for the safest repeatable demo. Switch it to `true` only after `make demo-preflight` confirms model access.

### 5. Deploy

```bash
cd terraform
terraform init -reconfigure
terraform apply -auto-approve
```

EKS provisioning takes ~10 minutes. On completion you'll have the `webhook_url` and all other outputs.

Because the repo now includes an S3 backend with `use_lockfile = true`, create or update the bucket settings in `terraform/backend.tf` before the first init if you are deploying into a different AWS account.

**Known issue:** Helm installs (Argo CD, Gatekeeper, observability) may fail on the first apply because the Kubernetes API is not yet reachable when Terraform's Helm provider initialises. Fix:

```bash
aws eks update-kubeconfig --name gitops-auto-remediation-cluster --region us-east-1
terraform apply -auto-approve   # second run succeeds
```

### 6. Configure Argo CD

```bash
# Get admin password
kubectl get secret argocd-initial-admin-secret -n argocd \
  -o jsonpath="{.data.password}" | base64 -d

# Port-forward and login
kubectl port-forward svc/argo-cd-argocd-server -n argocd 8080:443 &
argocd login localhost:8080 --username admin --password <PASSWORD> --insecure

# Register repo (public repo — no credentials needed)
argocd repo add https://github.com/YOUR_USERNAME/gitops-auto-remediation.git --insecure

# Verify the Terraform-bootstrapped apps and demo workload
make demo-preflight
```

### 7. GitHub Actions secrets

In your repo → Settings → Secrets → Actions, add:

| Secret | Value |
|---|---|
| `AWS_ACCESS_KEY_ID` | Your AWS key |
| `AWS_SECRET_ACCESS_KEY` | Your AWS secret |
| `AWS_REGION` | `us-east-1` |
| `AWS_EVENT_BUS_NAME` | `gitops-auto-remediation-bus` |

---

## Testing the Pipeline

### Fire a test alert

```bash
WEBHOOK_URL="<from terraform output>"
SECRET="<webhook_secret from tfvars>"

curl -s -X POST "$WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: $SECRET" \
  -d '{
    "receiver":"sentinel-webhook","status":"firing",
    "alerts":[{
      "status":"firing",
      "labels":{"alertname":"HighErrorRate","severity":"critical",
                "service":"demo-service","namespace":"demo-staging","env":"staging"},
      "annotations":{"summary":"High error rate on demo-service"},
      "startsAt":"2026-01-01T00:00:00Z","endsAt":"0001-01-01T00:00:00Z",
      "generatorURL":"http://prometheus:9090/graph"
    }],
    "groupLabels":{"alertname":"HighErrorRate"},
    "commonLabels":{"alertname":"HighErrorRate","severity":"critical",
                    "service":"demo-service","env":"staging"},
    "commonAnnotations":{"summary":"High error rate on demo-service"},
    "externalURL":"http://alertmanager:9093","version":"4",
    "groupKey":"{}/{alertname=~\"HighErrorRate\"}:{alertname=\"HighErrorRate\"}"
  }' | jq .
```

**Important:** Always include `"env":"staging"` in the alert labels. The Decision Engine uses this to build the correct kustomize overlay path (`gitops/apps/demo-service/overlays/{env}/kustomization.yaml`). Without it the path becomes `overlays/unknown/` which does not exist.

### Observe each stage

```bash
# Signal Collector
aws logs tail /aws/lambda/gitops-auto-remediation-signal-collector --since 5m --format short

# Decision Engine
aws logs tail /aws/lambda/gitops-auto-remediation-decision-engine --since 5m --format short

# PRs opened
gh pr list --repo YOUR_USERNAME/gitops-auto-remediation --state open

# After merging PR — replica count
kubectl get deployment demo-service -n demo-staging -o jsonpath='{.spec.replicas}'

# Outcome Validator
aws logs tail /aws/lambda/gitops-auto-remediation-outcome-validator --since 10m --format short

# DynamoDB Audit Log
aws dynamodb scan \
  --table-name gitops-auto-remediation-decision-audit \
  --query "Items[*].[incident_id.S, stage.S, action.S]" \
  --output table
```

### Dedup behaviour

The Signal Collector uses a DynamoDB conditional write keyed on a SHA-256 hash of `(alertname, service, labels)`. Identical alerts within 30 minutes return HTTP 202 without creating a new signal bundle. To test a new incident, change the `startsAt` timestamp.

---

## Customisation

### Adding a new allowed action

Edit `gitops/policies/allowed-actions.yaml`:

```yaml
allowed_actions:
  - action: my_new_action
    constraints:
      some_param: some_value
```

Then implement the action handler in `lambdas/decision_engine/app.py` inside the `handler()` function and update the heuristic fallback in `_choose_action_heuristic()`.

The `policy-check.yaml` GitHub Actions workflow enforces bounds — extend it if your new action has numeric constraints that should be validated at PR time.

### Changing the AI model

In `terraform.tfvars`:

```hcl
model_provider = "openai"   # or "bedrock"
```

For OpenAI, also add:
```hcl
openai_secret_arn = "arn:aws:secretsmanager:..."  # {"api_key":"sk-..."}
```

The Decision Engine tries Bedrock/OpenAI first and falls back to the heuristic if either fails or returns an unparseable response.

### Connecting real Prometheus

```hcl
# terraform.tfvars
prometheus_query_url = "http://your-prometheus:9090"
```

The Signal Collector queries (namespace and service are taken from the alert labels):
- `rate(http_requests_total{status=~"5.."}[5m])` — error rate
- `rate(container_cpu_usage_seconds_total{namespace="{namespace}",pod=~"{service}.*"}[5m])` — CPU
- `container_memory_working_set_bytes{namespace="{namespace}",pod=~"{service}.*"}` — memory

The Outcome Validator queries the same error rate metric post-remediation and validates it drops below 20%.

For Amazon Managed Prometheus (AMP), see `docs/amp-guidance.md` — SigV4 signing is required.

### Enabling the multi-agent pipeline

When `ENABLE_MULTI_AGENT=true` is set on the Signal Collector Lambda (controlled by `enable_multi_agent = true` in tfvars), the Signal Collector emits `SentinelPipelineTriggered` instead of `SignalBundled`, routing that incident through Step Functions instead of the single-agent Decision Engine path.

**Confidence scoring model:**
```
base_score = diagnosis_confidence  (0–100, from RootCauseAgent)

Penalties applied:
  severity == critical    →  -10
  blast_radius == high    →  -15
  action == scale_replicas → -5   (low risk, small penalty)
  action == rollback_image → -10  (medium risk)
  action == restart_rollout → -5

final_score = base_score - sum(penalties)

Routes:
  final_score >= 80 AND risk == low  →  auto_apply
  final_score >= 40                  →  open_pr
  final_score < 40                   →  escalate
```

For the live MVP demo, keep `enable_multi_agent = false` unless `make demo-preflight` confirms your chosen model provider is accessible.

---

## IAM Reference

Each Lambda has its own role (`${project_name}-{component}`) with a minimum-privilege inline policy:

| Lambda | Key permissions |
|---|---|
| Signal Collector | `s3:PutObject`, `events:PutEvents`, `dynamodb:PutItem/GetItem`, `eks:DescribeCluster` |
| Decision Engine | `s3:GetObject`, `secretsmanager:GetSecretValue`, `bedrock:InvokeModel`, `dynamodb:PutItem/UpdateItem` |
| Outcome Validator | `events:PutEvents`, `secretsmanager:GetSecretValue`, `dynamodb:PutItem/UpdateItem` |
| Agent Lambdas | `s3:GetObject`, `bedrock:InvokeModel`, `secretsmanager:GetSecretValue` |

> **Production hardening:** Resource ARNs are currently `*` for DynamoDB and Bedrock. Tighten to specific table ARNs and model ARNs. Add `aws:SourceArn` conditions on EventBridge → Lambda invoke permissions. See `docs/security-hardening.md` for the full checklist.

---

## CI/CD Workflows

| Workflow | Trigger | What it does |
|---|---|---|
| `validate-pr.yaml` | PR touching `gitops/**`, `observability/**`, `lambdas/**`, `terraform/**` | `kustomize build` both clusters, Lambda source sync check, `terraform validate`, `tflint`, `bandit`, and unit tests |
| `policy-check.yaml` | PR touching `gitops/**` | Validates replica count in kustomize overlay doesn't exceed `allowed-actions.yaml` max |
| `notify-action-dispatched.yaml` | Push to `main` with `gitops/**` changes | Extracts `inc-*` from commit message, fires `ActionDispatched` to EventBridge |

The `notify-action-dispatched.yaml` workflow bridges GitHub (where the PR merge happens) back to AWS EventBridge to trigger the Outcome Validator. This is the link that closes the feedback loop.

---

## Observability

- **CloudWatch Logs** — all Lambda functions emit structured JSON with a standardized schema. Always-present fields: `timestamp` (ISO-8601 UTC), `level`, `component`, `msg`, `request_id`, `trace_id`. Caller-supplied fields: `incident_id`, `service`, `env`, `alertname`, `execution_arn`. Example: `{"timestamp":"2026-05-27T14:03:11.482Z","level":"INFO","component":"signal_collector","msg":"incident_received","request_id":"abc-123","trace_id":"Root=1-...","incident_id":"inc-..."}`
- **Step Functions execution logs** — ALL-level execution events (state transitions, input/output) written to `/aws/vendedlogs/states/gitops-auto-remediation-multi-agent-pipeline` with 30-day retention
- **API Gateway access logs** — structured JSON per request (method, route, status, integration latency, error) in `/aws/apigateway/gitops-auto-remediation-webhook`
- **EKS control-plane logs** — api, audit, authenticator, controllerManager, scheduler all enabled with 180-day retention (security tier)
- **Pod logs** — `aws-for-fluent-bit` DaemonSet ships stdout/stderr from all namespaces to `/aws/eks/<cluster>/pod-logs`; pods annotated with `gitops.sentinel/incident-id` so logs are joinable on `incident_id`
- **X-Ray tracing** — enabled on all Lambda functions; trace the full execution path in the AWS Console
- **DynamoDB Audit Log** — `gitops-auto-remediation-decision-audit` table; every decision stored with 90-day TTL
- **EventBridge DLQ** — failed EventBridge deliveries land in an SQS dead letter queue (`gitops-auto-remediation-eventbridge-dlq`) with 14-day retention
- **Alerting** — CloudWatch metric filters on `webhook_auth_failed`, `dedup_write_failed`, `audit_write_failed`, `auto_revert_failed`, plus Lambda runtime alarms, Step Functions failure/timeout alarms, EventBridge DLQ depth alarms, and a monthly AWS budget notification; all notify the built-in SNS alarms topic and any extra `var.alarm_actions`
- **Grafana** — pipeline-health dashboard (incidents, dedup, PRs, outcomes, confidence routing, Lambda errors, SFN failures, DLQ depth) auto-provisioned via ConfigMap; CloudWatch datasource with IRSA for cross-pillar pivot

---

## Known Limitations and Production Gaps

| Gap | Impact | Recommended fix |
|---|---|---|
| Lambda packaging runs locally during `terraform apply` | Deployments depend on the operator machine having working `python3`, `pip`, and dependency download access | Move packaging to CI-built artifacts or a dedicated build pipeline if you need more reproducible deploys |
| IAM resource ARNs use `*` for DynamoDB and Bedrock | Over-permissive | Tighten to specific ARNs in the IAM module |
| `prometheus_query_url` is empty by default | Outcome Validator cannot verify recovery and will emit `OutcomeFailed` until Prometheus is reachable | Connect real Prometheus or AMP |
| Gatekeeper ConstraintTemplate must be synced before Constraint | Manual step required on fresh Argo CD setup | Add Argo CD sync waves via `argocd.argoproj.io/sync-wave` annotations |
| No Alertmanager integration test | You have to fire test alerts manually | Add a `make test-alert` Makefile target or a test receiver in Alertmanager config |
| Long-lived GitHub PAT | Security risk | Prefer a GitHub App installation-token flow if you move beyond the MVP |
| Lambda token cache (5-min TTL) not invalidated when Secrets Manager is updated | After rotating the GitHub token, the Lambda serves stale credentials for up to 5 minutes | Force a cold start immediately by updating any env var: `aws lambda update-function-configuration --function-name gitops-auto-remediation-decision-engine --environment "$(aws lambda get-function-configuration --function-name gitops-auto-remediation-decision-engine --query 'Environment' --output json \| python3 -c "import json,sys,time; e=json.load(sys.stdin); e['Variables']['CACHE_BUST']=str(time.time()); print(json.dumps(e))")"` |
| `lambdas/*/app.py` and `terraform/modules/lambda_*/src/app.py` can drift | CI fails the sync check, and contributors may be unsure which source tree is authoritative | Run `make sync-lambda` after changing Lambda code and commit both paths |
| `allowed-actions.yaml` must not be listed in `gitops/policies/kustomization.yaml` as a resource | It is a Lambda config file, not a Kubernetes manifest — kustomize will reject it with `missing Resource metadata` | Only Gatekeeper manifests belong in that kustomization's `resources:` list. `allowed-actions.yaml` is fetched directly from GitHub by the Decision Engine at runtime via the GitHub Contents API |

---

## Tear Down

```bash
cd terraform && terraform destroy -auto-approve

# Secrets Manager is not managed by Terraform — delete manually
aws secretsmanager delete-secret \
  --secret-id "gitops-auto-remediation/github-token" \
  --force-delete-without-recovery
```

If `terraform destroy` fails on the EKS node group (common when Kubernetes resources are still present), delete the Argo CD apps first:

```bash
argocd app delete demo-staging --cascade
argocd app delete demo-prod --cascade
kubectl delete namespace demo-staging
terraform destroy -auto-approve
```
