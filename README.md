# GitOps Sentinel

> Autonomous Kubernetes remediation — AI reasons about your incidents, Git owns every change, and humans only get paged when the system isn't sure.

---

## Why This Matters

On-call engineers get paged for everything — including incidents the system already knows how to fix. A pod OOMKills at 3 AM and someone wakes up to run `kubectl rollout restart`. A deployment drifts from the desired replica count and a Slack notification fires into a channel no one is watching.

The cost isn't just sleep. It's the cognitive overhead of triaging incidents that are routine, well-understood, and fixable in seconds by anyone who's seen them before.

GitOps Sentinel asks a different question: **what if the system could decide whether a human is actually needed?**

---

## What It Does

GitOps Sentinel is a confidence-gated remediation pipeline for Kubernetes. When an alert fires, it doesn't just notify — it reasons, proposes a fix, scores its own confidence, and acts accordingly.

| Confidence | Action |
|---|---|
| ≥ 80 + low risk | Auto-apply: the PR is opened and merged automatically |
| 40 – 79 | Open PR: a human reviews before anything touches the cluster |
| < 40 | Escalate: page on-call, no automated change |

Every remediation is a Git commit. The cluster never changes outside of a pull request or a high-confidence auto-apply — Argo CD syncs the rest. If the fix makes things worse, the system detects it and opens a revert PR automatically.

---

## What It Can Handle

**Incident types**
- Pod OOMKilled / crash-looping
- Deployment replica drift
- High HTTP error rate (5xx spike)
- Resource exhaustion (CPU/memory pressure)
- Bad image tag rollouts

**Remediation actions** (defined in `allowed-actions.yaml` — the LLM must choose from this list)
- `rollback_deployment` — revert to the last known-good image tag
- `scale_replicas` — adjust replica count within policy bounds
- `tune_resources` — update CPU/memory requests and limits
- `restart_pod` — targeted pod restart without full rollout

**Operational guarantees**
- Deduplicates alert storms: a 30-minute dedup window prevents the same incident from triggering multiple pipelines
- Validates every remediation: Prometheus health check 5 minutes post-merge; auto-reverts on failure
- Full audit trail: every decision, confidence score, and outcome written to DynamoDB (90-day retention)
- Two LLM providers supported: AWS Bedrock (Claude 3 Haiku) and OpenAI GPT-4 — switchable via config
- Two execution paths: a fast single-agent path for cost-sensitive environments and a full multi-agent pipeline for higher-quality decisions

---

## Architecture

At its core, GitOps Sentinel is a signal-to-action pipeline with a confidence gate in the middle:

```
Alert fires → Signal collected → AI pipeline → Confidence gate → Git commit → Cluster sync → Outcome check
```

In full:

```
Alertmanager / CloudWatch
        │
        ▼
  API Gateway (HMAC-validated webhook)
        │
        ▼
  Signal Collector Lambda          ◄── dedup via DynamoDB conditional write
        │  enriches: Prometheus metrics + k8s events → S3 bundle
        ▼
  EventBridge (custom bus + 7-day archive)
        │
        ├──► Decision Engine Lambda        (single-agent path, feature-flag off)
        │
        └──► Step Functions: Sentinel Pipeline  (multi-agent path, feature-flag on)
                │
                ├── Classifier Agent     → severity, blast radius, incident type
                ├── Root Cause Agent     → root cause, contributing factors, confidence
                ├── Action Planner       → action from allowed-actions.yaml + alternatives
                ├── Confidence Scorer    → deterministic score (no LLM), route decision
                └── RouteByConfidence ──► auto_apply | open_pr | escalate
                                              │
                                              ▼
                                    GitHub PR (GitOps write path)
                                              │
                                              ▼
                                    Argo CD syncs cluster
                                              │
                                              ▼
                              Outcome Validator Lambda
                                    (Prometheus health check)
                                              │
                                    ► auto-revert PR if OutcomeFailed
```

---

## How It Works

1. **Alert intake** — Alertmanager fires a signed webhook to API Gateway. HMAC validation rejects anything unsigned.

2. **Signal collection** — Signal Collector Lambda checks DynamoDB for a duplicate (same service + alert within 30 minutes). If new, it fetches Prometheus metrics and Kubernetes events, stores the enriched bundle in S3, and emits an event to EventBridge.

3. **Agent pipeline** — Behaviour is controlled by the `enable_multi_agent` flag:
   - **Single-agent** (`false`, default): Decision Engine reads the bundle, calls the LLM once, opens a GitHub PR. No confidence scoring. Designed for demos and cost-sensitive environments.
   - **Multi-agent** (`true`): Step Functions runs the full pipeline — Classifier → Root Cause → Action Planner → Confidence Scorer → RouteByConfidence.

4. **Confidence routing** — The Confidence Scorer produces a deterministic score (no LLM call, no latency) by starting from the diagnosis confidence and applying penalties for severity, blast radius, and action risk type. The score determines the route: auto-apply, open PR for review, or escalate to on-call.

5. **GitOps write** — The Decision Engine opens a PR against the GitOps repo targeting `gitops/apps/{service}/{deployment.yaml}`. High-confidence PRs are auto-merged via the GitHub API.

6. **Cluster sync** — Argo CD detects the merged commit and applies the change to the cluster. The cluster never changes outside of Git.

7. **Outcome validation** — Five minutes after the PR merges, the Outcome Validator queries Prometheus. If the error rate is still above 20%, it opens a revert PR automatically and emits `OutcomeFailed` to EventBridge.

---

## EventBridge Event Types

| Event | Emitted by | Triggers |
|---|---|---|
| `SignalBundled` | Signal Collector | Decision Engine (single-agent path) |
| `SentinelPipelineTriggered` | Signal Collector | Step Functions (multi-agent path) |
| `ActionDispatched` | Decision Engine | Outcome Validator |
| `OutcomeValidated` | Outcome Validator | — (terminal success) |
| `OutcomeFailed` | Outcome Validator | — (auto-revert initiated) |

---

## Demo

_Coming soon._

---

## Tech Stack

| Layer | Technology |
|---|---|
| Compute | AWS Lambda (Python 3.12), AWS Step Functions (Standard Workflow) |
| Event routing | AWS EventBridge (custom bus, DLQ, 7-day archive) |
| Storage | S3 (signal bundles), DynamoDB (dedup + audit log) |
| AI / LLM | AWS Bedrock (Claude 3 Haiku) · OpenAI GPT-4 |
| GitOps | Argo CD (Helm) |
| Kubernetes | EKS 1.33 |
| Observability | Prometheus + Grafana (Helm) |
| Policy | OPA / Gatekeeper (Helm) |
| Infrastructure | Terraform (18 custom modules) |
| Webhook auth | HMAC-SHA256 (API Gateway) |

---

## What's Actually Implemented

This is a portfolio project. Here's an honest account of what's built:

**Built and working**
- All 7 Lambda functions — Signal Collector, Decision Engine, Outcome Validator, Classifier Agent, Root Cause Agent, Action Planner, Confidence Scorer
- Full Step Functions state machine with retry and fallback logic
- GitHub PR automation: branch creation, commit, PR open, auto-merge
- Outcome validation with automatic revert PR
- DynamoDB deduplication and audit logging
- HMAC webhook validation
- 33 unit tests across all 7 functions
- 18 Terraform modules provisioning the full stack

**Intentional scope decisions**
- The `enable_multi_agent = false` default skips confidence scoring — suitable for demos, not production
- OPA/Gatekeeper is deployed but no ConstraintTemplate resources are defined out of the box; the `policy-check.yaml` CI step handles PR-time enforcement
- Lambda source exists in two places (`lambdas/` for local dev, `terraform/modules/lambda_*/src/` for deploy) — kept in sync manually; a build step would eliminate this
- Service name resolution derives from Alertmanager labels; alerts missing `service` or `namespace` labels fall back to `unknown`
- No webhook rate limiting beyond API Gateway defaults

---

## What's Next

_Coming soon._

---

## Local Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r lambdas/requirements-dev.txt

# Run tests
cd lambdas && pytest tests/ -v

# Lint
make lint

# Terraform
cd terraform && terraform init && terraform validate
terraform plan -var-file=terraform.tfvars
```

---

## Configuration

Copy `terraform/terraform.tfvars.example` to `terraform/terraform.tfvars`:

```hcl
github_owner            = "your-org"
github_repo             = "your-gitops-repo"
github_token_secret_arn = "arn:aws:secretsmanager:..."

prometheus_query_url = "https://prom.example.com"
slack_webhook_url    = "https://hooks.slack.com/..."
webhook_secret       = ""   # openssl rand -hex 32
enable_multi_agent   = true
model_provider       = "bedrock"  # or "openai"
```

---

## Deploying

```bash
cd terraform
terraform init
terraform apply -var-file=terraform.tfvars
```

The `webhook_url` output is the endpoint to configure in Alertmanager's `receivers`.

---

## Cost Estimate

| Component | ~Monthly (us-east-1) |
|---|---|
| EKS cluster (1.33, 2× t2.medium) | ~$140 |
| Lambda + Step Functions + EventBridge | < $10 |
| DynamoDB + S3 + API Gateway | < $5 |
| **Total** | **~$155–$178** |

---

## Project Structure

```
.
├── lambdas/
│   ├── signal_collector/       # Webhook ingestion + signal bundling
│   ├── decision_engine/        # Single-agent remediation coordinator
│   ├── outcome_validator/      # Post-remediation health check
│   ├── classifier_agent/       # Multi-agent: incident classification
│   ├── root_cause_agent/       # Multi-agent: root cause analysis
│   ├── action_planner/         # Multi-agent: remediation planning
│   ├── confidence_scorer/      # Multi-agent: deterministic scoring
│   └── tests/                  # Unit tests (7 Lambda functions covered)
├── terraform/
│   ├── main.tf                 # Root module
│   ├── variables.tf
│   ├── outputs.tf
│   └── modules/                # 18 custom Terraform modules
├── Makefile                    # install / test / lint / tf-* targets
└── docs/                       # Architecture diagrams, runbooks
```

---

## License

MIT
