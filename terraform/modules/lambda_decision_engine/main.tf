variable "project_name" { type = string }
variable "role_arn" { type = string }
variable "event_bus_name" { type = string }
variable "github_owner" { type = string }
variable "github_repo" { type = string }
variable "github_token_secret_arn" { type = string }
variable "openai_secret_arn" {
  type    = string
  default = ""
}
variable "model_provider" { type = string }
variable "bedrock_model_id" { type = string }
variable "aws_region" { type = string }
variable "cluster_name" { type = string }
variable "prometheus_query_url" { type = string }
variable "audit_table_name" { type = string }

module "package" {
  source            = "../lambda_package"
  function_name     = "decision_engine"
  source_dir        = "${path.root}/../lambdas/decision_engine"
  requirements_file = "${path.root}/../lambdas/requirements.txt"
}

resource "aws_lambda_function" "this" {
  function_name                  = "${var.project_name}-decision-engine"
  role                           = var.role_arn
  runtime                        = "python3.12"
  architectures                  = ["arm64"]
  handler                        = "app.handler"
  filename                       = module.package.filename
  source_code_hash               = module.package.source_code_hash
  timeout                        = 120
  reserved_concurrent_executions = 5

  tracing_config {
    mode = "Active"
  }

  environment {
    variables = {
      GITHUB_OWNER                = var.github_owner
      GITHUB_REPO                 = var.github_repo
      GITHUB_APP_TOKEN_SECRET_ARN = var.github_token_secret_arn
      EVENT_BUS_NAME              = var.event_bus_name
      MODEL_PROVIDER              = var.model_provider
      BEDROCK_MODEL_ID            = var.bedrock_model_id
      OPENAI_SECRET_ARN           = var.openai_secret_arn
      ALLOWED_ACTIONS_PATH        = "gitops/policies/allowed-actions.yaml"
      CLUSTER_NAME                = var.cluster_name
      PROMETHEUS_QUERY_URL        = var.prometheus_query_url
      AUDIT_TABLE_NAME            = var.audit_table_name
    }
  }
}

output "lambda_arn" { value = aws_lambda_function.this.arn }
output "lambda_name" { value = aws_lambda_function.this.function_name }
