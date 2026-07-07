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

# ── Monthly cost budget ───────────────────────────────────────────────────────
# Account-wide (project-tag filtering would require activating cost allocation
# tags in the billing console first). Mainly catches the EKS cluster being
# left running after a demo.

resource "aws_sns_topic_policy" "alarms" {
  arn = aws_sns_topic.alarms.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AccountOwnerDefault"
        Effect    = "Allow"
        Principal = { AWS = "*" }
        Action = [
          "SNS:Subscribe", "SNS:Receive", "SNS:Publish", "SNS:ListSubscriptionsByTopic",
          "SNS:GetTopicAttributes", "SNS:SetTopicAttributes", "SNS:DeleteTopic",
          "SNS:AddPermission", "SNS:RemovePermission",
        ]
        Resource  = aws_sns_topic.alarms.arn
        Condition = { StringEquals = { "AWS:SourceOwner" = data.aws_caller_identity.current.account_id } }
      },
      {
        Sid       = "AllowCloudWatchAlarms"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = "SNS:Publish"
        Resource  = aws_sns_topic.alarms.arn
      },
      {
        Sid       = "AllowBudgets"
        Effect    = "Allow"
        Principal = { Service = "budgets.amazonaws.com" }
        Action    = "SNS:Publish"
        Resource  = aws_sns_topic.alarms.arn
      },
    ]
  })
}

resource "aws_budgets_budget" "monthly" {
  name         = "${local.name}-monthly-cost"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 80
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.alarms.arn]
  }

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 15
    threshold_type            = "PERCENTAGE"
    notification_type         = "FORECASTED"
    subscriber_sns_topic_arns = [aws_sns_topic.alarms.arn]
  }
}
