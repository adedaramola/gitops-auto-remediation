# Cost Estimate — GitOps Auto-Remediation

> Costs vary by region, cluster size, and usage. This is a rough guide for a non-production deployment.

## Major cost drivers
- EKS: control plane hourly + worker nodes
- NAT Gateway (if enabled): hourly + data processing
- Lambda: invocations + duration (7 functions)
- Step Functions: state transitions (multi-agent pipeline)
- Bedrock: tokens per inference call (Classifier, Root Cause, Action Planner)
- EventBridge: custom events
- DynamoDB: on-demand reads/writes (dedup table + audit log table)
- S3: storage + PUT/GET (signal bundles)
- CloudWatch Logs + X-Ray traces

## Approximate monthly (light usage, us-east-1)
| Service | Estimate |
|---|---|
| EKS control plane | ~$73 |
| Worker nodes (2× t3.medium) | ~$60 |
| Lambda (< 1M invocations) | < $5 |
| Step Functions (< 1k executions) | < $1 |
| Bedrock (Claude Haiku, < 1M tokens) | < $5 |
| DynamoDB (on-demand) | < $3 |
| S3 + EventBridge + CloudWatch | < $5 |
| **Total** | **~$150/mo** |

## Tips to keep costs low
- Use small node groups (t3.medium or smaller)
- Keep NAT traffic minimal; consider public nodes for non-prod (security tradeoff)
- Set CloudWatch log retention to 7 days
- Use Bedrock on-demand pricing (no provisioned throughput needed at low volume)
- Tear down after demo: `terraform destroy`
