# GitOps Auto-Remediation — Architecture Diagrams

## 1. End-to-End Signal Flow

```mermaid
sequenceDiagram
    participant AM  as Alertmanager
    participant AGW as API Gateway
    participant SC  as Signal Collector
    participant DDB as DynamoDB (dedup)
    participant S3  as S3 (bundles)
    participant EB  as EventBridge
    participant DE  as Decision Engine
    participant SF  as Step Functions<br/>(Auto-Remediation Pipeline)
    participant GH  as GitHub
    participant CD  as Argo CD
    participant OV  as Outcome Validator
    participant AUD as DynamoDB (audit)

    AM->>AGW: POST /webhook (HMAC-signed)
    AGW->>SC: Invoke Lambda
    SC->>DDB: Conditional PutItem (dedup check)
    DDB-->>SC: OK / ConditionalCheckFailed
    SC->>S3: PutObject (signal bundle JSON)
    SC->>EB: PutEvents → SignalBundled or SentinelPipelineTriggered

    alt Single-agent path (enable_multi_agent=false)
        EB->>DE: Invoke Decision Engine
        DE->>S3: GetObject (bundle)
        DE->>GH: Create branch + commit
        DE->>GH: Open PR
        DE->>AUD: PutItem (stage=action_dispatched)
    else Multi-agent path (enable_multi_agent=true)
        EB->>SF: StartExecution
        SF->>SF: ClassifierAgent → RootCauseAgent → ActionPlannerAgent → ConfidenceScorer
        SF->>SF: RouteByConfidence
        alt confidence ≥ 80 and risk = low
            SF->>DE: Invoke Decision Engine (auto_apply route)
        else confidence 40-79
            SF->>DE: Invoke Decision Engine (open_pr route)
        else confidence < 40
            SF->>SF: EscalateToHuman (page on-call)
        end
    end

    GH->>CD: Webhook (push event)
    CD->>CD: Sync cluster

    Note over OV: ~5 min later
    GH->>EB: PutEvents → ActionDispatched (on merge to main)
    EB->>OV: Invoke Outcome Validator
    OV->>OV: Query Prometheus
    alt error_rate < 20%
        OV->>EB: PutEvents → OutcomeValidated
    else error_rate ≥ 20%
        OV->>GH: Open revert PR
        OV->>EB: PutEvents → OutcomeFailed
    end
    OV->>AUD: PutItem (stage=outcome_validated)
```

---

## 2. Confidence-Gated Routing (Step Functions)

```mermaid
flowchart TD
    A[SentinelPipelineTriggered] --> B[ClassifierAgent\nSeverity · BlastRadius · Priority]
    B --> C[RootCauseAgent\nRoot Cause · Contributing Factors\nDiagnosis Confidence 0-100]
    C --> D[ActionPlannerAgent\nProposed Action from allowed-actions.yaml\nAlternatives · Rationale]
    D --> E[ConfidenceScorer\nPure deterministic — no LLM\nBase = diagnosis_confidence\nPenalties: severity · blast_radius · action_type]
    E --> F{RouteByConfidence}
    F -->|confidence ≥ 80\nrisk = low| G[auto_apply route\nDecision Engine PR path]
    F -->|confidence 40-79| H[open_pr route\nDecision Engine PR path]
    F -->|confidence < 40| I[Escalate\nPage on-call\nNo cluster change]

    style G fill:#22c55e,color:#fff
    style H fill:#f59e0b,color:#fff
    style I fill:#ef4444,color:#fff
```

---

## 3. Audit Trail Write Path

```mermaid
sequenceDiagram
    participant DE  as Decision Engine
    participant OV  as Outcome Validator
    participant AUD as DynamoDB Audit Log<br/>(90-day TTL)

    DE->>AUD: PutItem\nstage=action_dispatched\naction, service, env, rationale\npr_url, outcome=pending

    Note over OV: Post-remediation health check
    OV->>AUD: PutItem\nstage=outcome_validated\noutcome=OutcomeValidated | OutcomeFailed\nrecovered, revert_url
```

---

## 4. EventBridge Event Topology

```mermaid
graph LR
    SC[Signal Collector] -->|SignalBundled| EB[(EventBridge\ncustom bus)]
    SC -->|SentinelPipelineTriggered| EB
    GH[GitHub Actions\nnotify-action-dispatched] -->|ActionDispatched| EB
    OV[Outcome Validator] -->|OutcomeValidated| EB
    OV -->|OutcomeFailed| EB

    EB -->|SignalBundled| DE
    EB -->|SentinelPipelineTriggered| SF[Step Functions]
    EB -->|ActionDispatched| OV

    EB --> DLQ[(SQS DLQ\n14-day retention)]
    EB --> ARC[(Event Archive\n7-day replay)]
```
