variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "log_retention_days" {
  type        = number
  default     = 30
  description = "Retention in days for runtime log groups (Lambda, API GW, SFN, pod logs)."
}

variable "audit_log_retention_days" {
  type        = number
  default     = 180
  description = "Retention in days for security/audit log groups (EKS control-plane audit, DynamoDB audit table indirectly)."
}

variable "enable_log_encryption" {
  type        = bool
  default     = false
  description = "When true, creates a KMS key and encrypts all managed CloudWatch log groups. Adds ~$1/month for the key."
}

variable "alarm_actions" {
  type        = list(string)
  default     = []
  description = "SNS topic ARNs to notify when a pipeline alarm fires. Leave empty to create alarms without notification."
}

variable "project_name" {
  type    = string
  default = "ai-gitops-Self-Healing"
}

# GitHub (for agent PRs)
variable "github_owner" {
  type = string
}

variable "github_repo" {
  type = string
}

# Store a GitHub token or GitHub App installation token JSON in Secrets Manager
variable "github_token_secret_arn" {
  type = string
}

# Model provider selection: bedrock|openai
variable "model_provider" {
  type    = string
  default = "bedrock"
}


# Networking / EKS
variable "cluster_name" {
  type    = string
  default = "ai-gitops-Cluster"
}

variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "az_count" {
  type    = number
  default = 2
}

# API Gateway webhook intake (Alertmanager -> API GW -> Bundler Lambda)
variable "enable_api_gateway" {
  type    = bool
  default = true
}

# Optional: Amazon Managed Service for Prometheus (AMP) for outcome_validator queries
variable "enable_amp" {
  type    = bool
  default = false
}


# Observability query endpoint (optional)
# - For AMP, you may provide an AMP query endpoint URL with SigV4 signing (not implemented here).
# - For self-managed Prometheus, expose Prometheus query API and provide its URL.
variable "prometheus_query_url" {
  type        = string
  default     = ""
  description = "Optional Prometheus query URL (e.g., https://prom.example.com). Used by Signal Collector and Outcome Validator."
}

# EKS cluster context for k8s read-only queries from Lambda (optional)
variable "enable_k8s_readonly_enrichment" {
  type        = bool
  default     = true
  description = "If true, Signal Collector queries Kubernetes API for events/deploy info using EKS auth token."
}

# Notifications (optional)
variable "slack_webhook_url" {
  type        = string
  default     = ""
  description = "Optional Slack webhook for outcome_validator status updates."
}

# Webhook authentication
variable "webhook_secret" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Shared secret for Alertmanager -> API Gateway webhook auth (X-Webhook-Secret header)."
}

# Multi-agent pipeline
variable "enable_multi_agent" {
  type        = bool
  default     = false
  description = "When true, routes incidents through the Step Functions multi-agent pipeline instead of the single decision_engine Lambda."
}
