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

variable "environment" {
  type        = string
  default     = "Stage"
  description = "Deployment environment tag. One of: Dev, Stage, Prod. Defaults to Stage because the MVP demo flow targets the staging GitOps overlay."
  validation {
    condition     = contains(["Dev", "Stage", "Prod"], var.environment)
    error_message = "environment must be Dev, Stage, or Prod."
  }
}

variable "alarm_actions" {
  type        = list(string)
  default     = []
  description = "Extra SNS topic ARNs to notify when a pipeline alarm fires, in addition to the topic this stack creates."
}

variable "alarm_email" {
  type        = string
  default     = ""
  description = "Optional email address subscribed to the pipeline alarms SNS topic. Requires confirming the subscription email after apply."
}

variable "monthly_budget_usd" {
  type        = number
  default     = 200
  description = "Monthly AWS cost budget (account-wide). Notifies the alarms SNS topic at 80% actual and 100% forecasted spend."
}

variable "auto_apply_max_per_hour" {
  type        = number
  default     = 3
  description = "Maximum PRs the Decision Engine may auto-merge per hour. Merges beyond this open PRs for human review instead."
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

variable "gitops_repo_revision" {
  type        = string
  default     = "main"
  description = "Git revision Argo CD should sync for the demo applications."
}

# Store a GitHub token or GitHub App installation token JSON in Secrets Manager
variable "github_token_secret_arn" {
  type = string
}

# Model provider selection: bedrock|openai
variable "model_provider" {
  type    = string
  default = "bedrock"
  validation {
    condition     = contains(["bedrock", "openai"], var.model_provider)
    error_message = "model_provider must be either bedrock or openai."
  }
}

variable "bedrock_model_id" {
  type        = string
  default     = "anthropic.claude-3-haiku-20240307-v1:0"
  description = "Foundation model ID used when model_provider = \"bedrock\"."
}

variable "openai_secret_arn" {
  type        = string
  default     = ""
  description = "Optional Secrets Manager ARN containing the OpenAI API key JSON payload."
  validation {
    condition     = var.model_provider != "openai" || trim(var.openai_secret_arn) != ""
    error_message = "openai_secret_arn must be set when model_provider is openai."
  }
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

variable "bootstrap_argocd_applications" {
  type        = bool
  default     = true
  description = "When true, Terraform creates the Argo CD Application resources for demo-staging, demo-prod, and platform-policies."
}

variable "kubernetes_api_ready_wait" {
  type        = string
  default     = "45s"
  description = "Extra wait after EKS creation before Helm/Kubernetes resources are applied. Helps avoid Unauthorized races on fresh clusters."
}

# Observability query endpoint (optional)
# - For AMP, you may provide an AMP query endpoint URL with SigV4 signing (not implemented here).
# - For self-managed Prometheus, expose Prometheus query API and provide its URL.
variable "prometheus_query_url" {
  type        = string
  default     = ""
  description = "Optional Prometheus query URL (e.g., https://prom.example.com). Used by Signal Collector and Outcome Validator."
}

variable "enable_private_prometheus_endpoint" {
  type        = bool
  default     = false
  description = "When true, exposes Prometheus on a private VPC-only load balancer and attaches telemetry Lambdas to the VPC."
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
