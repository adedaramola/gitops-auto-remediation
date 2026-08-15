# GitOps Auto-Remediation — Junior Developer Implementation Guide

This guide walks you through deploying GitOps Auto-Remediation from scratch. Follow every step in order. Each section explains **what** you are doing and **why**, so you understand the system as you build it.

---

## What Is GitOps Auto-Remediation?

GitOps Auto-Remediation is a self-healing infrastructure platform. When a production alert fires (e.g. high error rate), the system:

1. Receives the alert via webhook
2. Analyses it using an AI agent (AWS Bedrock by default, OpenAI optional)
3. Proposes a remediation (e.g. scale replicas) as a **GitHub Pull Request**
4. After the PR is merged, Argo CD applies the change to the Kubernetes cluster
5. The Outcome Validator checks if the service recovered

**Key principle:** The AI never touches the cluster directly. All changes go through Git pull requests, giving you full audit trail and rollback capability.

---

## Architecture Overview

```
Alertmanager → API Gateway → Signal Collector Lambda
                                      ↓
                 EventBridge (SignalBundled or SentinelPipelineTriggered)
                         ↙ single-agent                    ↘ multi-agent
      Decision Engine Lambda (Bedrock/OpenAI)      Step Functions pipeline
                         ↓                                   ↓
                              GitHub Pull Request
                                      ↓
                           GitHub Actions (CI checks)
                                      ↓
                            Argo CD syncs cluster
                                      ↓
                          Outcome Validator Lambda
                          (checks Prometheus metrics)
```

---

## Prerequisites

Before you start, make sure you have the following installed and configured on your machine.

### Tools Required

| Tool | Version | Install |
|---|---|---|
| AWS CLI | v2 | https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html |
| Terraform | >= 1.10 | https://developer.hashicorp.com/terraform/install |
| kubectl | >= 1.28 | https://kubernetes.io/docs/tasks/tools/ |
| Helm | >= 3.12 | https://helm.sh/docs/intro/install/ |
| Argo CD CLI | latest | `brew install argocd` |
| GitHub CLI | latest | `brew install gh` |
| Python | 3.12 | https://python.org/downloads |
| Git | any | pre-installed on most systems |

### Accounts Required

- **AWS account** with admin access (or at minimum the permissions listed in the IAM section below)
- **GitHub account**
- An **AWS region** — this guide uses `us-east-1`

### AWS IAM Permissions Needed

Your AWS user/role must be able to create:
- EKS clusters and node groups
- Lambda functions
- API Gateway (HTTP API)
- EventBridge buses and rules
- DynamoDB tables
- S3 buckets
- Secrets Manager secrets
- IAM roles and policies
- SQS queues
- Step Functions state machines
- VPC, subnets, NAT Gateway

> **Tip for junior devs:** If you are using a sandbox AWS account you control, attach `AdministratorAccess` to your IAM user for simplicity. Never do this in a production account.

---

## Step 1 — Fork and Clone the Repository

```bash
# Fork the repo on GitHub first (click Fork at the top right of the repo page)
# Then clone YOUR fork (replace YOUR_USERNAME):
git clone https://github.com/YOUR_USERNAME/gitops-auto-remediation.git
cd gitops-auto-remediation
```

> **Why fork?** The Decision Engine Lambda will open pull requests against your GitHub repo. It needs to be YOUR repo so it has permission to write to it.

If you plan to use Terraform Argo bootstrap, set your repo values in `terraform/terraform.tfvars` instead of editing Argo manifests by hand. The local manifests under `gitops/argocd/` remain useful as references and fallbacks.

---

## Step 2 — Configure AWS CLI

```bash
aws configure
```

Enter your:
- AWS Access Key ID
- AWS Secret Access Key
- Default region: `us-east-1`
- Default output format: `json`

Verify it works:

```bash
aws sts get-caller-identity
```

You should see your AWS account ID and user ARN.

---

## Step 3 — Set Up Python Virtual Environment

The Lambda unit tests run locally. Set up a Python virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r lambdas/requirements-dev.txt
```

Run the tests to confirm everything is healthy before you deploy:

```bash
cd lambdas
pytest tests/ -v
```

You should see the full Lambda test suite pass. If any tests fail, stop here and fix them before continuing.

```bash
cd ..  # back to repo root
```

---

## Step 4 — Create a GitHub Token

The Decision Engine Lambda needs a GitHub bearer token so it can open pull requests on your behalf.

For the MVP, a classic PAT with `repo` scope is the simplest option. If you already have a GitHub App installation-token flow, that works too because the runtime just reads `{ "token": "..." }` from Secrets Manager.

1. Go to **GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)**
2. Click **Generate new token (classic)**
3. Give it a name: `gitops-auto-remediation-decision-engine`
4. Set expiration: 90 days (or no expiration for a long-running demo)
5. Select scopes: ✅ **repo** (this includes contents:write and pull_requests:write)
6. Click **Generate token**
7. **Copy the token immediately** — you won't see it again

---

## Step 5 — Create the GitHub Token Secret in AWS

Store the token in AWS Secrets Manager so the Lambda can retrieve it securely:

```bash
aws secretsmanager create-secret \
  --name "gitops-auto-remediation/github-token" \
  --description "GitHub token for GitOps Auto-Remediation PR creation" \
  --secret-string '{"token":"YOUR_GITHUB_TOKEN_HERE"}' \
  --region us-east-1
```

Note the `ARN` in the output — you will need it in the next step. It looks like:
```
arn:aws:secretsmanager:us-east-1:123456789012:secret:gitops-auto-remediation/github-token-xxxxxx
```

---

## Step 6 — Create the Terraform Variables File

Copy the example file:

```bash
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
```

Edit `terraform/terraform.tfvars` and fill in your values:

```hcl
# ─── Required ─────────────────────────────────────────────────────────────────

aws_region   = "us-east-1"
project_name = "gitops-auto-remediation"
cluster_name = "gitops-auto-remediation-cluster"

# GitHub — use YOUR username and repo name
github_owner = "YOUR_GITHUB_USERNAME"
github_repo  = "gitops-auto-remediation"

# ARN from Step 5 above
github_token_secret_arn = "arn:aws:secretsmanager:us-east-1:ACCOUNT_ID:secret:gitops-auto-remediation/github-token-xxxxxx"

# ─── Model provider ───────────────────────────────────────────────────────────

model_provider = "bedrock"

# ─── Networking ───────────────────────────────────────────────────────────────

vpc_cidr = "10.20.0.0/16"
az_count  = 2

# ─── Observability ────────────────────────────────────────────────────────────

prometheus_query_url           = ""     # leave empty for initial testing
enable_k8s_readonly_enrichment = true

# ─── Notifications ────────────────────────────────────────────────────────────

slack_webhook_url = ""   # optional — leave empty if you don't have Slack
alarm_email      = ""    # optional — confirm the AWS email subscription after apply
monthly_budget_usd      = 200
auto_apply_max_per_hour = 3

# ─── Webhook security ─────────────────────────────────────────────────────────

# Generate a strong random secret:
# Run: openssl rand -hex 32
webhook_secret = "PASTE_OUTPUT_OF_OPENSSL_COMMAND_HERE"

# ─── Multi-agent pipeline ─────────────────────────────────────────────────────

enable_multi_agent = false
```

> **Important:** `terraform.tfvars` is in `.gitignore` — it will never be committed. Never put real secrets in files that get committed to Git.

> **Safest demo default:** start with `enable_multi_agent = false`. Switch it to `true` only after `make demo-preflight` confirms model access in your AWS account.

---

## Step 7 — Lambda Packaging

No manual dependency copy step is required now. Terraform packages each Lambda directly from `lambdas/<function>/` during apply using the shared packaging flow in `terraform/modules/lambda_package/` and `terraform/scripts/build_lambda.sh`.

---

## Step 8 — Add Helm Repositories

Terraform uses Helm to install Argo CD, Gatekeeper, and kube-prometheus-stack onto the cluster. Add the Helm repos first:

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add argo https://argoproj.github.io/argo-helm
helm repo update
```

---

## Step 9 — Deploy with Terraform

```bash
cd terraform

# Review terraform/backend.tf first if you are using a different AWS account.
# The repo now defaults to an S3 remote backend with native lockfile locking.

# Initialise Terraform (downloads providers and modules and configures backend)
terraform init -reconfigure

# Preview what will be created
terraform plan

# Deploy everything
terraform apply -auto-approve
```

This will take **10–15 minutes** — most of the time is EKS cluster provisioning.

When it finishes you will see outputs like:

```
cluster_endpoint        = "https://XXXX.gr7.us-east-1.eks.amazonaws.com"
cluster_name            = "gitops-auto-remediation-cluster"
decision_engine_lambda  = "gitops-auto-remediation-decision-engine"
event_bus_name          = "gitops-auto-remediation-bus"
alarms_topic_arn        = "arn:aws:sns:us-east-1:123456789012:gitops-auto-remediation-alarms"
auto_apply_kill_switch  = "/gitops-auto-remediation/auto-apply-enabled"
outcome_validator_lambda = "gitops-auto-remediation-outcome-validator"
signal_collector_lambda = "gitops-auto-remediation-signal-collector"
signals_bucket_name     = "gitops-auto-remediation-incidents-xxxxxxxx"
signals_table_name      = "gitops-auto-remediation-incidents"
webhook_url             = "https://XXXXXXXXXX.execute-api.us-east-1.amazonaws.com/webhook"
```

**Save these outputs** — you will use them throughout the remaining steps.

```bash
cd ..  # back to repo root
```

---

## Step 10 — Connect kubectl to the Cluster

After Terraform finishes, configure kubectl to talk to your new EKS cluster:

```bash
aws eks update-kubeconfig --name gitops-auto-remediation-cluster --region us-east-1
```

Verify both nodes are Ready:

```bash
kubectl get nodes
```

Expected output:
```
NAME                           STATUS   ROLES    AGE   VERSION
ip-10-20-x-x.ec2.internal      Ready    <none>   5m    v1.34.x-eks-xxxxx
ip-10-20-x-x.ec2.internal      Ready    <none>   5m    v1.34.x-eks-xxxxx
```

If nodes show `NotReady`, wait another 2–3 minutes and try again.

---

## Step 11 — Set Up Argo CD

### Connect the Argo CD CLI

First, get the Argo CD admin password:

```bash
kubectl get secret argocd-initial-admin-secret -n argocd \
  -o jsonpath="{.data.password}" | base64 -d
```

Port-forward the Argo CD server so you can connect:

```bash
kubectl port-forward svc/argo-cd-argocd-server -n argocd 8080:443 &
```

Login:

```bash
argocd login localhost:8080 --username admin --password YOUR_PASSWORD --insecure
```

### Register Your GitHub Repository

```bash
argocd repo add https://github.com/YOUR_USERNAME/gitops-auto-remediation.git --insecure
```

> **Note:** If your repo is private, add credentials:
> ```bash
> argocd repo add https://github.com/YOUR_USERNAME/gitops-auto-remediation.git \
>   --username YOUR_USERNAME \
>   --password YOUR_GITHUB_TOKEN \
>   --insecure
> ```

### Apply the Argo CD Applications

```bash
make demo-preflight
```

That verifies the Terraform-bootstrapped Argo CD apps are synced and that `demo-service` is healthy in `demo-staging`.

Check both apps are Synced and Healthy manually if you want:

```bash
kubectl get applications -n argocd
```

Expected:
```
NAME           SYNC STATUS   HEALTH STATUS
demo-prod      Synced        Healthy
demo-staging   Synced        Healthy
```

Check the demo pods are running:

```bash
kubectl get pods -n demo-staging
```

Expected (2 running pods):
```
NAME                            READY   STATUS    RESTARTS   AGE
demo-service-xxxxxxxxx-xxxxx    1/1     Running   0          1m
demo-service-xxxxxxxxx-xxxxx    1/1     Running   0          1m
```

---

## Step 12 — Configure GitHub Actions Secret

The `notify-action-dispatched` GitHub Actions workflow needs to know your EventBridge bus name to fire the `ActionDispatched` event when a remediation PR is merged.

1. Go to your GitHub repo → **Settings → Secrets and variables → Actions**
2. Click **New repository secret**
3. Add these secrets:

| Secret name | Value |
|---|---|
| `AWS_ACCESS_KEY_ID` | Your AWS access key |
| `AWS_SECRET_ACCESS_KEY` | Your AWS secret key |
| `AWS_REGION` | `us-east-1` |
| `AWS_EVENT_BUS_NAME` | `gitops-auto-remediation-bus` (from Terraform output) |

---

## Step 13 — Fire a Test Alert (End-to-End Test)

Now test the full pipeline. The `webhook_url` comes from the Terraform output in Step 9.

```bash
WEBHOOK_URL="https://YOUR_WEBHOOK_URL/webhook"
WEBHOOK_SECRET="YOUR_WEBHOOK_SECRET"   # value from terraform.tfvars

PAYLOAD='{
  "receiver": "sentinel-webhook",
  "status": "firing",
  "alerts": [{
    "status": "firing",
    "labels": {
      "alertname": "HighErrorRate",
      "severity": "critical",
      "service": "demo-service",
      "namespace": "demo-staging",
      "env": "staging"
    },
    "annotations": {
      "summary": "High error rate on demo-service",
      "description": "Error rate above 50% for 5 minutes"
    },
    "startsAt": "2026-01-01T00:00:00Z",
    "endsAt": "0001-01-01T00:00:00Z",
    "generatorURL": "http://prometheus:9090/graph"
  }],
  "groupLabels": {"alertname": "HighErrorRate"},
  "commonLabels": {
    "alertname": "HighErrorRate",
    "severity": "critical",
    "service": "demo-service",
    "env": "staging"
  },
  "commonAnnotations": {"summary": "High error rate on demo-service"},
  "externalURL": "http://alertmanager:9093",
  "version": "4",
  "groupKey": "{}/{alertname=~\"HighErrorRate\"}:{alertname=\"HighErrorRate\"}"
}'

curl -s -X POST "$WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
  -d "$PAYLOAD" | jq .
```

**Expected response:**
```json
{
  "incident_id": "inc-1234567890-abcdef12",
  "s3_key": "incidents/inc-1234567890-abcdef12.json"
}
```

---

## Step 14 — Watch the Pipeline Execute

### 1. Check the Signal Collector Lambda logs
```bash
aws logs tail /aws/lambda/gitops-auto-remediation-signal-collector \
  --since 5m --format short
```
Look for a JSON log line with `"event": "signal_bundled"`.

### 2. Check the Decision Engine Lambda logs
```bash
aws logs tail /aws/lambda/gitops-auto-remediation-decision-engine \
  --since 5m --format short
```
Look for `"event": "pr_opened"` or `"event": "pr_already_exists"`.

### 3. Check for the GitHub PR
```bash
gh pr list --repo YOUR_USERNAME/gitops-auto-remediation --state open
```

You should see a PR like:
```
[AI] inc-1234567890: scale_replicas for demo-service (staging)
```

### 4. Review and merge the PR
```bash
# View the diff
gh pr diff 1 --repo YOUR_USERNAME/gitops-auto-remediation

# Merge it
gh pr merge 1 --repo YOUR_USERNAME/gitops-auto-remediation --squash
```

### 5. Watch Argo CD apply the change
```bash
# Wait ~15 seconds after merging, then:
kubectl get deployment demo-service -n demo-staging -o jsonpath='{.spec.replicas}'
```

The replica count should have changed (e.g. 2 → 3).

### 6. Check the Outcome Validator Lambda logs
After the GitHub Actions workflow runs (it fires `ActionDispatched` to EventBridge on merge):
```bash
aws logs tail /aws/lambda/gitops-auto-remediation-outcome-validator \
  --since 10m --format short
```

### 7. Check the DynamoDB Audit Log
```bash
aws dynamodb scan \
  --table-name gitops-auto-remediation-decision-audit \
  --query "Items[*].[incident_id.S, stage.S, action.S]" \
  --output table
```

---

## Common Errors and Fixes

| Symptom | Likely cause | Fix |
|---|---|---|
| `No module named 'requests'` in Lambda logs | Local Lambda packaging did not complete successfully during `terraform apply` | Re-run `terraform apply` after confirming `python3` and `pip` are available locally; packaging is handled by `terraform/scripts/build_lambda.sh` |
| `GitHub API error 403` in Decision Engine logs | GitHub token lacks repo write permissions | Create a token with `repo` scope or equivalent app permissions, then update Secrets Manager |
| `GitHub API error 403` after updating the token | Lambda has cached the old token (5-min TTL) | Force a cold start by updating any env var on the Lambda: `aws lambda update-function-configuration --function-name gitops-auto-remediation-decision-engine --environment "$(aws lambda get-function-configuration --function-name gitops-auto-remediation-decision-engine --query 'Environment' --output json \| python3 -c "import json,sys,time; e=json.load(sys.stdin); e['Variables']['CACHE_BUST']=str(time.time()); print(json.dumps(e))")"` then fire a new alert |
| Argo CD app shows `ComparisonError: allowed-actions.yaml missing Resource metadata` | `allowed-actions.yaml` is a policy config file, not a Kubernetes manifest — it must not be listed as a kustomize resource | Check `gitops/policies/kustomization.yaml` — the `resources:` list should only contain `gatekeeper/constrainttemplate-deployment-bounds.yaml` and `gatekeeper/constraint-deployment-bounds.yaml`. Remove any reference to `allowed-actions.yaml` |
| Argo CD app shows `ComparisonError` (other kustomize errors) | kustomize build fails | Run `kustomize build gitops/clusters/staging` locally to see the full error |
| `K8sDeploymentBounds CRD not installed` on Argo CD sync | ConstraintTemplate not applied first | Run `argocd app sync demo-staging --resource templates.gatekeeper.sh:ConstraintTemplate:k8sdeploymentbounds` then full sync |
| Webhook returns `Internal Server Error` | Lambda import error | Check Lambda logs: `aws logs tail /aws/lambda/gitops-auto-remediation-signal-collector --since 5m` |
| Signal Collector returns 401 | Wrong `X-Webhook-Secret` header value | Check `webhook_secret` in `terraform.tfvars` matches the header you're sending |
| DynamoDB dedup suppressing alerts | Same alert fired within 30-min TTL window | Change `startsAt` timestamp in the test payload to get a new `incident_id` |
| Step Functions pipeline stuck | Agent Lambda timeout or Bedrock throttling | Check X-Ray traces in AWS Console → X-Ray → Traces |
| Terraform `Error: Module not installed` | New module added, not initialised | Run `terraform init` then `terraform apply` |
| EKS nodes `NotReady` after apply | Nodes still bootstrapping | Wait 3–5 minutes and retry `kubectl get nodes` |

---

## Tear Down (Important — Avoid Unexpected Costs)

When you are done testing, destroy all AWS resources:

```bash
cd terraform
terraform destroy -auto-approve
```

This takes 10–15 minutes. Verify in the AWS Console that:
- The EKS cluster is gone
- The S3 bucket is gone (you may need to empty it first if it has objects)
- The Lambda functions are gone

> **Note:** The Secrets Manager secret (`gitops-auto-remediation/github-token`) is NOT managed by Terraform — delete it manually:
> ```bash
> aws secretsmanager delete-secret \
>   --secret-id "gitops-auto-remediation/github-token" \
>   --force-delete-without-recovery
> ```

---

## What to Explore Next

Once you have the basic loop working:

1. **Connect real Prometheus** — set `prometheus_query_url` in `terraform.tfvars` to your Prometheus endpoint. The Signal Collector will enrich alerts with real error rate metrics, and the Outcome Validator will check actual recovery.

2. **Try the multi-agent pipeline** — the Step Functions pipeline (Classifier → Root Cause → Action Planner → Confidence Scorer) runs when `enable_multi_agent = true`. Enable it only after `make demo-preflight` confirms working model access.

3. **Add Slack notifications** — set `slack_webhook_url` in `terraform.tfvars` to get notified when the Outcome Validator runs.

4. **Extend `allowed-actions.yaml`** — add new allowed remediation actions and see how the Decision Engine respects the contract.

5. **Review the DynamoDB Audit Log** — every AI decision is recorded with full context. This is your full audit trail for every autonomous change made to the cluster.
