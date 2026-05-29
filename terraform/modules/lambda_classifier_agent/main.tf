variable "project_name" { type = string }
variable "role_arn" { type = string }
variable "aws_region" { type = string }
variable "model_provider" { type = string }
variable "bedrock_model_id" { type = string }
variable "openai_secret_arn" {
  type    = string
  default = ""
}

module "package" {
  source            = "../lambda_package"
  function_name     = "classifier_agent"
  source_dir        = "${path.root}/../lambdas/classifier_agent"
  requirements_file = "${path.root}/../lambdas/requirements.txt"
}

resource "aws_lambda_function" "this" {
  function_name                  = "${var.project_name}-classifier-agent"
  role                           = var.role_arn
  runtime                        = "python3.12"
  architectures                  = ["arm64"]
  handler                        = "app.handler"
  filename                       = module.package.filename
  source_code_hash               = module.package.source_code_hash
  timeout                        = 60
  reserved_concurrent_executions = 5

  tracing_config { mode = "Active" }

  environment {
    variables = {
      MODEL_PROVIDER    = var.model_provider
      BEDROCK_MODEL_ID  = var.bedrock_model_id
      AWS_REGION_NAME   = var.aws_region
      OPENAI_SECRET_ARN = var.openai_secret_arn
    }
  }
}

output "lambda_arn" { value = aws_lambda_function.this.arn }
output "lambda_name" { value = aws_lambda_function.this.function_name }
