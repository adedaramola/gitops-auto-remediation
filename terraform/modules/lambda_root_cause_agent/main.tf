variable "project_name" { type = string }
variable "role_arn" { type = string }
variable "aws_region" { type = string }
variable "model_provider" { type = string }
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
  function_name                  = "${var.project_name}-root-cause-agent"
  role                           = var.role_arn
  runtime                        = "python3.12"
  architectures                  = ["arm64"]
  handler                        = "app.handler"
  filename                       = data.archive_file.zip.output_path
  source_code_hash               = data.archive_file.zip.output_base64sha256
  timeout                        = 60
  reserved_concurrent_executions = 5

  tracing_config { mode = "Active" }

  environment {
    variables = {
      MODEL_PROVIDER    = var.model_provider
      AWS_REGION_NAME   = var.aws_region
      OPENAI_SECRET_ARN = var.openai_secret_arn
    }
  }
}

output "lambda_arn" { value = aws_lambda_function.this.arn }
output "lambda_name" { value = aws_lambda_function.this.function_name }
