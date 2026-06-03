# GitOps Auto-Remediation — Security Hardening

Recommended hardening steps before running in production.

## API Gateway
- Add HMAC webhook secret (`webhook_secret` variable) — already implemented in Signal Collector
- Add AWS WAF rule group to the API Gateway stage
- Enable throttling and rate limiting
- Validate payload schema at the gateway level

## GitHub Authentication
- Replace static PAT with GitHub App installation token flow — already implemented (token cache in Decision Engine)
- Restrict app permissions to:
  - `Contents: write` (only necessary paths)
  - `Pull requests: write`
- Enforce branch protection + required CI checks on the GitOps repo

## Network
- Lambda functions run without VPC by default (public egress + strict IAM)
- If using OpenAI, restrict outbound via NAT + egress firewalling / DNS controls
- Consider VPC placement if Prometheus is internal-only

## Data
- Use KMS CMK for S3 signal bundles and Secrets Manager secrets
- Lock down S3 bucket policy to Signal Collector Lambda ARN only
- Consider encrypting signal bundles — they contain alert labels that may include service names and environments

## IAM
- Resource ARNs in IAM policies are currently `*` for DynamoDB and Bedrock — tighten to specific table ARNs and model ARNs in production
- Separate roles per Lambda already implemented; review and minimise further
- Add `aws:SourceArn` condition on EventBridge → Lambda invoke permissions

## Audit & Observability
- DynamoDB Audit Log records every decision with 90-day TTL — extend retention via DynamoDB Streams → S3 if required
- X-Ray tracing enabled on all Lambda functions — review traces in AWS Console after each incident
- Enable SQS DLQ CloudWatch alarms to catch failed EventBridge deliveries
