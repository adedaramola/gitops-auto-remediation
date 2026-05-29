variable "project_name" { type = string }
variable "role_arn" { type = string }
variable "incident_bucket_name" { type = string }
variable "event_bus_name" { type = string }
variable "aws_region" { type = string }
variable "cluster_name" { type = string }
variable "prometheus_query_url" { type = string }
variable "enable_k8s_readonly_enrichment" { type = bool }
variable "incidents_table_name" { type = string }
variable "webhook_secret" {
  type      = string
  default   = ""
  sensitive = true
}
variable "enable_multi_agent" {
  type    = bool
  default = false
}
variable "audit_table_name" { type = string }
variable "subnet_ids" {
  type    = list(string)
  default = []
}
variable "security_group_ids" {
  type    = list(string)
  default = []
}

module "package" {
  source            = "../lambda_package"
  function_name     = "signal_collector"
  source_dir        = "${path.root}/../lambdas/signal_collector"
  requirements_file = "${path.root}/../lambdas/requirements.txt"
}

resource "aws_lambda_function" "this" {
  function_name                  = "${var.project_name}-signal-collector"
  role                           = var.role_arn
  runtime                        = "python3.12"
  architectures                  = ["arm64"]
  handler                        = "app.handler"
  filename                       = module.package.filename
  source_code_hash               = module.package.source_code_hash
  timeout                        = 60
  reserved_concurrent_executions = 10

  tracing_config {
    mode = "Active"
  }

  dynamic "vpc_config" {
    for_each = length(var.subnet_ids) > 0 && length(var.security_group_ids) > 0 ? [1] : []
    content {
      subnet_ids         = var.subnet_ids
      security_group_ids = var.security_group_ids
    }
  }

  environment {
    variables = {
      DEDUP_TTL_SECONDS    = "1800"
      INCIDENTS_TABLE_NAME = var.incidents_table_name
      INCIDENT_BUCKET      = var.incident_bucket_name
      EVENT_BUS_NAME       = var.event_bus_name
      CLUSTER_NAME         = var.cluster_name
      PROMETHEUS_QUERY_URL = var.prometheus_query_url
      ENABLE_K8S_READONLY  = tostring(var.enable_k8s_readonly_enrichment)
      WEBHOOK_SECRET       = var.webhook_secret
      ENABLE_MULTI_AGENT   = tostring(var.enable_multi_agent)
      AUDIT_TABLE_NAME     = var.audit_table_name
    }
  }
}

output "lambda_arn" { value = aws_lambda_function.this.arn }
output "lambda_name" { value = aws_lambda_function.this.function_name }
