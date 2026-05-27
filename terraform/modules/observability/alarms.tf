# CloudWatch Metric Filters and Alarms for critical pipeline failure events.
# Metric filters extract named events from Lambda JSON logs; alarms fire when
# they breach threshold within a 5-minute evaluation window.

variable "alarm_actions" {
  type        = list(string)
  default     = []
  description = "SNS topic ARNs to notify when an alarm transitions to ALARM state."
}

locals {
  lambda_log_groups = [
    "/aws/lambda/${var.project_name}-signal-collector",
    "/aws/lambda/${var.project_name}-decision-engine",
    "/aws/lambda/${var.project_name}-outcome-validator",
    "/aws/lambda/${var.project_name}-classifier-agent",
    "/aws/lambda/${var.project_name}-root-cause-agent",
    "/aws/lambda/${var.project_name}-action-planner",
    "/aws/lambda/${var.project_name}-confidence-scorer",
  ]

  # Single-log-group filters: events emitted by specific Lambdas.
  single_filters = {
    webhook_auth_failed = {
      log_group   = "/aws/lambda/${var.project_name}-signal-collector"
      pattern     = "{ $.msg = \"webhook_auth_failed\" }"
      description = "Webhook HMAC/Bearer auth rejected — possible misconfigured Alertmanager or probe."
    }
    dedup_write_failed = {
      log_group   = "/aws/lambda/${var.project_name}-signal-collector"
      pattern     = "{ $.msg = \"dedup_write_failed\" }"
      description = "DynamoDB conditional write failed — dedup table may be throttled or unavailable."
    }
    audit_write_failed = {
      log_group   = "/aws/lambda/${var.project_name}-decision-engine"
      pattern     = "{ $.msg = \"audit_write_failed\" }"
      description = "DynamoDB audit log write failed — decision may be untracked."
    }
    auto_revert_failed = {
      log_group   = "/aws/lambda/${var.project_name}-outcome-validator"
      pattern     = "{ $.msg = \"auto_revert_failed\" }"
      description = "Auto-revert PR could not be opened — manual intervention required."
    }
  }
}

# ── Per-Lambda metric filters for the four failure events ─────────────────────
resource "aws_cloudwatch_log_metric_filter" "single" {
  for_each = local.single_filters

  name           = "${var.project_name}-${each.key}"
  log_group_name = each.value.log_group
  pattern        = each.value.pattern

  metric_transformation {
    name          = each.key
    namespace     = "GitOpsSentinel/Alerts"
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

# ── Lambda ERROR filter across every Lambda log group ────────────────────────
resource "aws_cloudwatch_log_metric_filter" "lambda_errors" {
  for_each = toset(local.lambda_log_groups)

  name           = "${var.project_name}-lambda-error-${replace(replace(each.value, "/aws/lambda/${var.project_name}-", ""), "/", "-")}"
  log_group_name = each.value
  pattern        = "{ $.level = \"ERROR\" }"

  metric_transformation {
    name          = "LambdaError"
    namespace     = "GitOpsSentinel/Alerts"
    value         = "1"
    default_value = "0"
    unit          = "Count"
    dimensions    = { component = "$.component" }
  }
}

# ── Alarms for single-event filters (threshold = 1) ──────────────────────────
resource "aws_cloudwatch_metric_alarm" "single" {
  for_each = local.single_filters

  alarm_name          = "${var.project_name}-${each.key}"
  alarm_description   = each.value.description
  namespace           = "GitOpsSentinel/Alerts"
  metric_name         = each.key
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = var.alarm_actions
  ok_actions    = var.alarm_actions
}

# ── Alarm for repeated Lambda exceptions (>= 3 errors in 5 min) ──────────────
resource "aws_cloudwatch_metric_alarm" "lambda_error_rate" {
  alarm_name          = "${var.project_name}-lambda-error-rate"
  alarm_description   = "3 or more Lambda ERROR logs in 5 minutes across any function."
  namespace           = "GitOpsSentinel/Alerts"
  metric_name         = "LambdaError"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 3
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = var.alarm_actions
  ok_actions    = var.alarm_actions
}

# ── Alarm for EventBridge DLQ depth ──────────────────────────────────────────
resource "aws_cloudwatch_metric_alarm" "eventbridge_dlq" {
  alarm_name          = "${var.project_name}-eventbridge-dlq-depth"
  alarm_description   = "Messages in the EventBridge DLQ — at least one event was not delivered."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = "${var.project_name}-eventbridge-dlq" }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = var.alarm_actions
  ok_actions    = var.alarm_actions
}
