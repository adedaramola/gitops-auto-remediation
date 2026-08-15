# GitOps Auto-Remediation — Security Hardening

Recommended hardening steps before running in production.

## API Gateway
- Add HMAC webhook secret (`webhook_secret` variable) — already implemented in Signal Collector
- Add AWS WAF rule group to the API Gateway stage
- Enable stricter API Gateway throttling and rate limiting; only the auto-apply merge path has a built-in hourly limiter today
- Validate payload schema at the gateway level

## GitHub Authentication
- The runtime accepts any bearer token stored as `{ "token": "..." }` in Secrets Manager. For production, prefer a GitHub App installation-token flow over a long-lived PAT.
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
- Encrypt the Terraform remote-state bucket and block public access if you keep the default S3 backend
- Lock down S3 bucket policy to Signal Collector Lambda ARN only
- Consider encrypting signal bundles — they contain alert labels that may include service names and environments

## IAM
- Resource ARNs in IAM policies are currently `*` for DynamoDB and Bedrock — tighten to specific table ARNs and model ARNs in production
- Separate roles per Lambda already implemented; review and minimise further
- Add `aws:SourceArn` condition on EventBridge → Lambda invoke permissions

## Audit & Observability
- DynamoDB Audit Log records every decision with 90-day TTL — extend retention via DynamoDB Streams → S3 if required
- X-Ray tracing enabled on all Lambda functions — review traces in AWS Console after each incident
- CloudWatch alarms already cover Lambda runtime failures, Step Functions failures/timeouts, and EventBridge DLQ depth; subscribe the SNS alarms topic to your paging path
- AWS Budgets already publishes monthly cost notifications to the same SNS alarms topic; tune `monthly_budget_usd` to your actual spend tolerance
