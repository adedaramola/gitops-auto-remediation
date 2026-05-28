variable "project_name" { type = string }
variable "role_arn" { type = string }
variable "aws_region" { type = string }
variable "cluster_name" { type = string }
variable "prometheus_query_url" { type = string }
variable "slack_webhook_url" { type = string }
variable "event_bus_name" { type = string }
variable "github_owner" { type = string }
variable "github_repo" { type = string }
variable "github_token_secret_arn" { type = string }
variable "audit_table_name" { type = string }

data "archive_file" "zip" {
  type        = "zip"
  source_dir  = "${path.module}/src"
  output_path = "${path.module}/function.zip"
}

resource "aws_lambda_function" "this" {
  function_name                  = "${var.project_name}-outcome-validator"
  role                           = var.role_arn
  runtime                        = "python3.12"
  architectures                  = ["arm64"]
  handler                        = "app.handler"
  filename                       = data.archive_file.zip.output_path
  source_code_hash               = data.archive_file.zip.output_base64sha256
  timeout                        = 60
  reserved_concurrent_executions = 5

  tracing_config {
    mode = "Active"
  }

  environment {
    variables = {
      GITHUB_APP_TOKEN_SECRET_ARN = var.github_token_secret_arn
      GITHUB_REPO                 = var.github_repo
      GITHUB_OWNER                = var.github_owner
      AUTO_REVERT_ON_FAIL         = "true"
      CLUSTER_NAME                = var.cluster_name
      PROMETHEUS_QUERY_URL        = var.prometheus_query_url
      SLACK_WEBHOOK_URL           = var.slack_webhook_url
      EVENT_BUS_NAME              = var.event_bus_name
      AUDIT_TABLE_NAME            = var.audit_table_name
    }
  }
}

output "lambda_arn" { value = aws_lambda_function.this.arn }
output "lambda_name" { value = aws_lambda_function.this.function_name }
