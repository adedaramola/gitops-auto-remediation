# GitOps Auto-Remediation — Lambda Functions

## signal_collector
- Receives Alertmanager webhook payload (via API Gateway)
- Validates HMAC webhook secret
- Deduplicates signals via DynamoDB conditional write (30-min TTL window)
- Enriches with Prometheus snapshot queries and Kubernetes events (optional, read-only)
- Writes signal bundle to S3
- Emits `SignalBundled` or `AutoRemediationPipelineTriggered` to EventBridge

## decision_engine
- Triggered by `SignalBundled` (single-agent path)
- Reads signal bundle from S3
- Loads `allowed-actions.yaml` contract from GitOps repo
- Chooses a remediation action via Bedrock (default) or OpenAI, with heuristic fallback
- Checks for existing PR (idempotency) before opening a new one
- Writes `action_dispatched` record to DynamoDB Audit Log

## outcome_validator
- Triggered by `ActionDispatched` after a remediation PR is merged to `main` and the GitHub Actions workflow emits the event back to EventBridge
- Checks recovery via PromQL (error rate < 20% threshold)
- Emits `OutcomeValidated` or `OutcomeFailed`
- Opens revert PR automatically on failure
- Posts Slack notification (optional)
- Writes `outcome_validated` record to DynamoDB Audit Log

---

## Multi-agent pipeline (Step Functions)

### classifier_agent
- Classifies incident: severity class, blast radius, priority, key signals

### root_cause_agent
- LLM root cause analysis using classifier output + signal bundle
- Returns root cause, contributing factors, diagnosis confidence (0–100)

### action_planner
- Proposes remediation action from `allowed-actions.yaml`
- Returns action, rationale, and alternatives

### confidence_scorer
- Pure deterministic scoring — no LLM, no added latency
- Base score = diagnosis confidence
- Penalties: severity, blast radius, action type
- Routes to: `auto_apply` (≥80 + low risk) / `open_pr` (40–79) / `escalate` (<40)

---

## Running tests
```bash
cd lambdas
python -m pytest tests/ -v
```
Run the current Lambda test suite locally with no real AWS credentials required.
