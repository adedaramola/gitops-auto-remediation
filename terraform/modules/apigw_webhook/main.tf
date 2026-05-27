variable "project_name" { type = string }
variable "bundler_lambda_arn" { type = string }
variable "bundler_lambda_name" { type = string }
variable "log_retention_days" {
  type    = number
  default = 30
}
variable "kms_key_arn" {
  type    = string
  default = null
}

resource "aws_apigatewayv2_api" "this" {
  name          = "${var.project_name}-webhook"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.this.id
  integration_type       = "AWS_PROXY"
  integration_uri        = var.bundler_lambda_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "post" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "POST /webhook"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_cloudwatch_log_group" "access_logs" {
  name              = "/aws/apigateway/${var.project_name}-webhook"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
  tags              = { Project = var.project_name }
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.this.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.access_logs.arn
    format = jsonencode({
      timestamp           = "$context.requestTime"
      apigw_request_id    = "$context.requestId"
      http_method         = "$context.httpMethod"
      route               = "$context.routeKey"
      status              = "$context.status"
      response_length     = "$context.responseLength"
      source_ip           = "$context.identity.sourceIp"
      user_agent          = "$context.identity.userAgent"
      protocol            = "$context.protocol"
      integration_status  = "$context.integrationStatus"
      integration_latency = "$context.integrationLatency"
      integration_error   = "$context.integrationErrorMessage"
      error_message       = "$context.error.message"
      error_response_type = "$context.error.responseType"
    })
  }
}

resource "aws_lambda_permission" "apigw_invoke" {
  statement_id  = "AllowExecutionFromApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = var.bundler_lambda_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.this.execution_arn}/*/*"
}

output "webhook_url" {
  value = "${aws_apigatewayv2_api.this.api_endpoint}/webhook"
}

output "access_log_group_name" {
  value = aws_cloudwatch_log_group.access_logs.name
}

output "access_log_group_arn" {
  value = aws_cloudwatch_log_group.access_logs.arn
}
