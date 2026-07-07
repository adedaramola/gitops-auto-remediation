# Pipeline health monitoring: SNS notification topic + alarms on signals the
# log-based filters in modules/observability/alarms.tf cannot see (Lambda
# runtime failures like timeouts/OOM, and Step Functions execution failures).

resource "aws_sns_topic" "alarms" {
  name = "${local.name}-alarms"
  tags = { Project = local.name }
}

resource "aws_sns_topic_subscription" "alarm_email" {
  count = var.alarm_email != "" ? 1 : 0

  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

locals {
  alarm_action_arns = concat(var.alarm_actions, [aws_sns_topic.alarms.arn])
}

# ── AWS/Lambda Errors per function ────────────────────────────────────────────
# Catches unhandled exceptions, timeouts, and OOM kills that never reach the
# structured-log filters (those only match JSON lines the handler itself logs).
resource "aws_cloudwatch_metric_alarm" "lambda_function_errors" {
  for_each = toset(local.lambda_function_names)

  alarm_name          = "${each.value}-errors"
  alarm_description   = "Lambda ${each.value} reported invocation errors (unhandled exception, timeout, or OOM)."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = each.value }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_action_arns
  ok_actions    = local.alarm_action_arns

  tags = { Project = local.name }
}

# ── Step Functions pipeline failures ──────────────────────────────────────────
resource "aws_cloudwatch_metric_alarm" "sfn_executions_failed" {
  alarm_name          = "${local.name}-pipeline-executions-failed"
  alarm_description   = "Multi-agent remediation pipeline execution failed."
  namespace           = "AWS/States"
  metric_name         = "ExecutionsFailed"
  dimensions          = { StateMachineArn = module.sentinel_pipeline.state_machine_arn }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_action_arns
  ok_actions    = local.alarm_action_arns

  tags = { Project = local.name }
}

resource "aws_cloudwatch_metric_alarm" "sfn_executions_timed_out" {
  alarm_name          = "${local.name}-pipeline-executions-timed-out"
  alarm_description   = "Multi-agent remediation pipeline execution timed out."
  namespace           = "AWS/States"
  metric_name         = "ExecutionsTimedOut"
  dimensions          = { StateMachineArn = module.sentinel_pipeline.state_machine_arn }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_action_arns
  ok_actions    = local.alarm_action_arns

  tags = { Project = local.name }
}

output "alarms_topic_arn" {
  value       = aws_sns_topic.alarms.arn
  description = "SNS topic all pipeline alarms notify. Subscribe email/Slack forwarders here."
}
