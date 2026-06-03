# GitOps Auto-Remediation

> Autonomous Kubernetes remediation — AI reasons about your incidents, Git owns every change, and humans only get paged when the system isn't sure.

---

## Why This Matters

On-call engineers get paged for everything — including incidents the system already knows how to fix. A pod OOMKills at 3 AM and someone wakes up to run `kubectl rollout restart`. A deployment drifts from the desired replica count and a Slack notification fires into a channel no one is watching.

The cost isn't just sleep. It's the cognitive overhead of triaging incidents that are routine, well-understood, and fixable in seconds by anyone who's seen them before.

GitOps Auto-Remediation asks a different question: **what if the system could decide whether a human is actually needed?**

---

## What It Does

GitOps Auto-Remediation is a confidence-gated remediation pipeline for Kubernetes. When an alert fires, it doesn't just notify — it reasons, proposes a fix, scores its own confidence, and acts accordingly.

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

At its core, GitOps Auto-Remediation is a signal-to-action pipeline with a confidence gate in the middle:

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
        └──► Step Functions: Auto-Remediation Pipeline  (multi-agent path, feature-flag on)
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

4. **Confidence routing** — The Confidence Scorer produces a deterministic score (no LLM call, no latency) by starting from the diagnosis confidence and applying penalties for severity, blast radius, and action risk type. The score determines the route: fast-track PR, open PR for review, or escalate to on-call.

5. **GitOps write** — The Decision Engine opens a PR against the GitOps repo targeting `gitops/apps/{service}/{deployment.yaml}`. The safe demo flow continues after a human merges that PR.

6. **Cluster sync** — Argo CD detects the merged commit and applies the change to the cluster. The cluster never changes outside of Git.

7. **Outcome validation** — Five minutes after the PR merges, the Outcome Validator queries Prometheus. If the error rate is still above 20%, it opens a revert PR automatically and emits `OutcomeFailed` to EventBridge.

---

## EventBridge Event Types

| Event | Emitted by | Triggers |
|---|---|---|
| `SignalBundled` | Signal Collector | Decision Engine (single-agent path) |
| `AutoRemediationPipelineTriggered` | Signal Collector | Step Functions (multi-agent path) |
| `ActionDispatched` | GitHub Actions merge workflow | Outcome Validator |
| `OutcomeValidated` | Outcome Validator | — (terminal success) |
| `OutcomeFailed` | Outcome Validator | — (auto-revert initiated) |

---

## Demo

For the MVP demo, focus on one end-to-end story:

1. Trigger a `HighHTTP5xxErrorRate` alert for `demo-service`
2. Show Signal Collector writing the enriched incident bundle to S3
3. Show the GitHub PR against the GitOps repo
4. Merge the PR and let Argo CD sync it
5. Show the GitHub Actions handoff that emits `ActionDispatched`
6. Show Outcome Validator reporting `OutcomeValidated` or `OutcomeFailed`
7. If preflight confirms model access, optionally show the multi-agent Step Functions execution as an advanced path

This is the clearest path to demonstrate the product's core claim: AI can reason about a Kubernetes incident, write a GitOps change, and validate the result without direct cluster write access.

See [docs/demo-script.md](docs/demo-script.md) for the talk track.
Use [docs/demo-alert.json](docs/demo-alert.json) with `make demo-alert` for the fastest live trigger.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Compute | AWS Lambda (Python 3.12), AWS Step Functions (Standard Workflow) |
| Event routing | AWS EventBridge (custom bus, DLQ, 7-day archive) |
| Storage | S3 (signal bundles), DynamoDB (dedup + audit log) |
| AI / LLM | AWS Bedrock (Claude 3 Haiku) · OpenAI GPT-4 |
| GitOps | Argo CD (Helm) |
| Kubernetes | EKS 1.34 |
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
- GitHub PR automation: branch creation, commit, PR open, idempotency checks
- Outcome validation with automatic revert PR
- DynamoDB deduplication and audit logging
- HMAC webhook validation
- 33 unit tests across all 7 functions
- 18 Terraform modules provisioning the full stack

**Intentional scope decisions**
- The `enable_multi_agent = false` default skips confidence scoring — suitable for demos, not production
- OPA/Gatekeeper is deployed but no ConstraintTemplate resources are defined out of the box; the `policy-check.yaml` CI step handles PR-time enforcement
- Terraform packages Lambda deploy artifacts directly from `lambdas/` using a local build step, so the machine running `terraform apply` needs `python3` and `pip`
- Service name resolution derives from Alertmanager labels; alerts missing `service` or `namespace` labels fall back to `unknown`
- No webhook rate limiting beyond API Gateway defaults

---

## MVP Scope

For a live demo, treat GitOps Auto-Remediation as an MVP with one primary success path:

- Incident: `HighHTTP5xxErrorRate`
- Service: `demo-service`
- Routing mode: `enable_multi_agent = true` only after `make demo-preflight` confirms model access; otherwise use `false`
- LLM provider: whichever passes `make demo-preflight`
- Outcome: open a GitHub PR, reconcile via Argo CD, then verify with Outcome Validator

What to emphasize in the demo:

- Confidence-gated routing is real
- The system writes changes through Git, not directly to the cluster
- Every decision is captured in S3, Step Functions, GitHub, and DynamoDB

What not to overclaim in the demo:

- This is not a hardened production deployment
- Not every incident type has been exercised end to end
- Safety controls are present, but some operational hardening is intentionally out of scope for the MVP

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
gitops_repo_revision    = "main"
github_token_secret_arn = "arn:aws:secretsmanager:..."

prometheus_query_url = "https://prom.example.com"
slack_webhook_url    = "https://hooks.slack.com/..."
webhook_secret       = ""   # openssl rand -hex 32
bootstrap_argocd_applications = true
enable_multi_agent   = true
model_provider       = "bedrock"  # or "openai"
bedrock_model_id     = "anthropic.claude-3-haiku-20240307-v1:0"
openai_secret_arn    = ""   # required only when model_provider = "openai"
```

Recommended MVP profile:

```hcl
bootstrap_argocd_applications  = true
enable_private_prometheus_endpoint = true
enable_k8s_readonly_enrichment = true
```

Then choose one of these demo modes:

- Safest repeatable demo: `enable_multi_agent = false`
- Full confidence-gated demo: `enable_multi_agent = true`, but only after `make demo-preflight` confirms your model provider works

---

## Deploying

```bash
cd terraform
terraform init
terraform apply -var-file=terraform.tfvars
```

The `webhook_url` output is the endpoint to configure in Alertmanager's `receivers`.

Before a live demo, run:

```bash
make demo-preflight
```

That checks Argo CD application sync, `demo-service` readiness in `demo-staging`, and whether multi-agent mode has working model access.

Fastest MVP trigger after deploy:

```bash
make demo-alert \
  WEBHOOK_URL="<webhook_url_from_terraform>" \
  WEBHOOK_SECRET="<webhook_secret_if_configured>"
```

---

## Cost Estimate

| Component | ~Monthly (us-east-1) |
|---|---|
| EKS cluster (1.34, 2× t2.medium) | ~$140 |
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
