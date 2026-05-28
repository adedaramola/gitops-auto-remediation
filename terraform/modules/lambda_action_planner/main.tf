variable "project_name" { type = string }
variable "role_arn" { type = string }
variable "aws_region" { type = string }
variable "model_provider" { type = string }
variable "github_owner" { type = string }
variable "github_repo" { type = string }
variable "github_token_secret_arn" { type = string }
variable "openai_secret_arn" {
  type    = string
  default = ""
}

data "archive_file" "zip" {
  type        = "zip"
  source_dir  = "${path.module}/src"
  output_path = "${path.module}/function.zip"
}

resource "aws_lambda_function" "this" {
  function_name                  = "${var.project_name}-action-planner"
  role                           = var.role_arn
  runtime                        = "python3.12"
  architectures                  = ["arm64"]
  handler                        = "app.handler"
  filename                       = data.archive_file.zip.output_path
  source_code_hash               = data.archive_file.zip.output_base64sha256
  timeout                        = 90
  reserved_concurrent_executions = 5

  tracing_config { mode = "Active" }

  environment {
    variables = {
      MODEL_PROVIDER              = var.model_provider
      AWS_REGION_NAME             = var.aws_region
      GITHUB_OWNER                = var.github_owner
      GITHUB_REPO                 = var.github_repo
      GITHUB_APP_TOKEN_SECRET_ARN = var.github_token_secret_arn
      OPENAI_SECRET_ARN           = var.openai_secret_arn
    }
  }
}

output "lambda_arn" { value = aws_lambda_function.this.arn }
output "lambda_name" { value = aws_lambda_function.this.function_name }
