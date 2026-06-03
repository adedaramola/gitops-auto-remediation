# GitOps Auto-Remediation — Demo Script

## Goal
Show one believable MVP path end to end: the system reasons about an incident, scores its confidence, opens a GitOps change, and validates the outcome **without** giving any agent direct cluster write access.

Recommended incident for the demo:
- `HighHTTP5xxErrorRate`
- Service: `demo-service`
- Mode: `enable_multi_agent = false` for the safest repeatable demo
- Expected artifact trail: S3 bundle -> GitHub PR -> Argo CD sync -> Outcome Validator result
- Optional advanced path: switch `enable_multi_agent = true` only after `make demo-preflight` confirms model access, then show the Step Functions execution as part of the same story

Recommended trigger options:
- `make demo-alert WEBHOOK_URL="<webhook_url>" WEBHOOK_SECRET="<secret>"`
- `curl` directly with [docs/demo-alert.json](demo-alert.json)

## 5-minute narrative

1. **Show dashboards** — Prometheus/Grafana, highlight the `HighHTTP5xxErrorRate` alert for `demo-service`
2. **Show webhook endpoint** — `webhook_url` from Terraform output, point to API Gateway
3. **Trigger a simulated alert** — POST to the webhook (or fire a real Alertmanager alert)
4. **Show Signal Collector output** — open the S3 signal bundle JSON, highlight enriched context (Prometheus metrics, k8s events)
5. **Show GitHub PR** — created by the Decision Engine, point to `allowed-actions.yaml` constraints and the exact file changed in GitOps
6. **Merge PR → Argo CD syncs** — show Argo CD UI reconciling the cluster
7. **Show GitHub Actions handoff** — mention that merge to `main` emits `ActionDispatched` back to EventBridge
8. **Show Outcome Validator** — Prometheus health check result, `OutcomeValidated` or `OutcomeFailed`, DynamoDB Audit Log entry
9. **Optional multi-agent view** — if `enable_multi_agent = true` passed preflight, show the Step Functions execution and the RouteByConfidence decision

## Copy-paste trigger

```bash
make demo-alert \
  WEBHOOK_URL="<webhook_url_from_terraform>" \
  WEBHOOK_SECRET="<webhook_secret_if_configured>"
```

Direct `curl` equivalent:

```bash
curl -X POST "$WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
  --data @docs/demo-alert.json
```

## Key talking points
- GitOps is the execution engine; the agent pipeline is the reasoning engine
- **Confidence-gated routing** means the system knows when it's sure enough to act autonomously
- Gatekeeper + CI = hard safety rails — no agent can push a change Gatekeeper rejects
- DynamoDB dedup prevents alert storms from triggering duplicate remediations
- Rollback is automated via revert PR, not imperative cluster writes
- Every decision is recorded in the DynamoDB Audit Log with full traceability

## MVP framing
- The demo proves the product thesis, not every edge case
- We are intentionally optimizing for one polished path over broad production hardening
- If asked about production readiness, position this as a strong MVP with clear next hardening steps
