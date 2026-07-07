variable "aws_region" { type = string }
variable "model_provider" { type = string }
variable "bedrock_model_id" { type = string }
variable "cluster_arn" { type = string }
variable "project_name" { type = string }
variable "incident_bucket_arn" { type = string }
variable "event_bus_arn" { type = string }
variable "incidents_table_arn" { type = string }
variable "audit_table_arn" { type = string }
variable "auto_apply_param_arn" { type = string }
variable "github_token_secret_arn" { type = string }
variable "openai_secret_arn" {
  type    = string
  default = ""
}

# ── Trust policies ────────────────────────────────────────────────────────────
data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "assume_lambda" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "assume_sfn" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "assume_events" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

# ── Core Lambda roles ─────────────────────────────────────────────────────────
resource "aws_iam_role" "signal_collector" {
  name               = "${var.project_name}-signal-collector"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

resource "aws_iam_role" "decision_engine" {
  name               = "${var.project_name}-decision-engine"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

resource "aws_iam_role" "outcome_validator" {
  name               = "${var.project_name}-outcome-validator"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

# ── Multi-agent Lambda roles ──────────────────────────────────────────────────
resource "aws_iam_role" "classifier_agent" {
  name               = "${var.project_name}-classifier-agent"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

resource "aws_iam_role" "root_cause_agent" {
  name               = "${var.project_name}-root-cause-agent"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

resource "aws_iam_role" "action_planner" {
  name               = "${var.project_name}-action-planner"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

resource "aws_iam_role" "confidence_scorer" {
  name               = "${var.project_name}-confidence-scorer"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

# ── Step Functions role ───────────────────────────────────────────────────────
resource "aws_iam_role" "sfn" {
  name               = "${var.project_name}-sfn"
  assume_role_policy = data.aws_iam_policy_document.assume_sfn.json
}

# ── EventBridge → Step Functions role ────────────────────────────────────────
resource "aws_iam_role" "events_sfn" {
  name               = "${var.project_name}-events-sfn"
  assume_role_policy = data.aws_iam_policy_document.assume_events.json
}

# ── Attach basic execution + X-Ray to all Lambda roles ───────────────────────
locals {
  lambda_roles = {
    signal_collector  = aws_iam_role.signal_collector.name
    decision_engine   = aws_iam_role.decision_engine.name
    outcome_validator = aws_iam_role.outcome_validator.name
    classifier_agent  = aws_iam_role.classifier_agent.name
    root_cause_agent  = aws_iam_role.root_cause_agent.name
    action_planner    = aws_iam_role.action_planner.name
    confidence_scorer = aws_iam_role.confidence_scorer.name
  }

  vpc_lambda_roles = {
    signal_collector  = aws_iam_role.signal_collector.name
    outcome_validator = aws_iam_role.outcome_validator.name
  }

  # Geo-prefixed model IDs (us./eu./apac.) are cross-region inference
  # profiles: invocation needs the account-scoped profile ARN plus the
  # underlying foundation-model ARN in every region the profile routes to.
  bedrock_is_inference_profile = can(regex("^(us|eu|apac)\\.", var.bedrock_model_id))
  bedrock_foundation_model_id  = replace(var.bedrock_model_id, "/^(us|eu|apac)\\./", "")

  bedrock_model_arns = var.model_provider == "bedrock" ? (
    local.bedrock_is_inference_profile ? [
      "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.bedrock_model_id}",
      "arn:aws:bedrock:*::foundation-model/${local.bedrock_foundation_model_id}",
    ] : [
      "arn:aws:bedrock:${var.aws_region}::foundation-model/${var.bedrock_model_id}"
    ]
  ) : []
  openai_secret_arns = var.openai_secret_arn != "" ? [var.openai_secret_arn] : []
  github_secret_arns = var.github_token_secret_arn != "" ? [var.github_token_secret_arn] : []
  llm_secret_arns    = var.model_provider == "openai" ? local.openai_secret_arns : []
}

resource "aws_iam_role_policy_attachment" "basic" {
  for_each   = local.lambda_roles
  role       = each.value
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "xray" {
  for_each   = local.lambda_roles
  role       = each.value
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_iam_role_policy_attachment" "vpc_access" {
  for_each   = local.vpc_lambda_roles
  role       = each.value
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "lambda_cloudwatch_metrics" {
  for_each = local.lambda_roles
  name     = "${var.project_name}-${each.key}-cw-metrics"
  role     = each.value
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["cloudwatch:PutMetricData"], Resource = ["*"] }
    ]
  })
}

# ── Inline policies ───────────────────────────────────────────────────────────
resource "aws_iam_role_policy" "signal_collector_inline" {
  name = "${var.project_name}-signal-collector-inline"
  role = aws_iam_role.signal_collector.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:PutObject", "s3:PutObjectAcl"], Resource = ["${var.incident_bucket_arn}/*"] },
      { Effect = "Allow", Action = ["events:PutEvents"], Resource = [var.event_bus_arn] },
      { Effect = "Allow", Action = ["dynamodb:PutItem"], Resource = [var.incidents_table_arn] },
      { Effect = "Allow", Action = ["eks:DescribeCluster"], Resource = [var.cluster_arn] }
    ]
  })
}

resource "aws_iam_role_policy" "decision_engine_inline" {
  name = "${var.project_name}-decision-engine-inline"
  role = aws_iam_role.decision_engine.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        { Effect = "Allow", Action = ["s3:GetObject"], Resource = ["${var.incident_bucket_arn}/*"] },
        { Effect = "Allow", Action = ["events:PutEvents"], Resource = [var.event_bus_arn] },
        { Effect = "Allow", Action = ["dynamodb:PutItem", "dynamodb:UpdateItem"], Resource = [var.audit_table_arn] },
        { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = [var.auto_apply_param_arn] }
      ],
      length(concat(local.github_secret_arns, local.llm_secret_arns)) > 0 ? [
        { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = concat(local.github_secret_arns, local.llm_secret_arns) }
      ] : [],
      length(local.bedrock_model_arns) > 0 ? [
        { Effect = "Allow", Action = ["bedrock:InvokeModel"], Resource = local.bedrock_model_arns }
      ] : [],
    )
  })
}

resource "aws_iam_role_policy" "outcome_validator_inline" {
  name = "${var.project_name}-outcome-validator-inline"
  role = aws_iam_role.outcome_validator.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        { Effect = "Allow", Action = ["events:PutEvents"], Resource = [var.event_bus_arn] },
        { Effect = "Allow", Action = ["dynamodb:PutItem"], Resource = [var.audit_table_arn] },
        { Effect = "Allow", Action = ["eks:DescribeCluster"], Resource = [var.cluster_arn] }
      ],
      length(local.github_secret_arns) > 0 ? [
        { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = local.github_secret_arns }
      ] : [],
    )
  })
}

# ── Multi-agent inline policies ───────────────────────────────────────────────
resource "aws_iam_role_policy" "classifier_inline" {
  name = "${var.project_name}-classifier-inline"
  role = aws_iam_role.classifier_agent.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        { Effect = "Allow", Action = ["s3:GetObject"], Resource = ["${var.incident_bucket_arn}/*"] }
      ],
      length(local.bedrock_model_arns) > 0 ? [
        { Effect = "Allow", Action = ["bedrock:InvokeModel"], Resource = local.bedrock_model_arns }
      ] : [],
      length(local.llm_secret_arns) > 0 ? [
        { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = local.llm_secret_arns }
      ] : [],
    )
  })
}

resource "aws_iam_role_policy" "root_cause_inline" {
  name = "${var.project_name}-root-cause-inline"
  role = aws_iam_role.root_cause_agent.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        { Effect = "Allow", Action = ["s3:GetObject"], Resource = ["${var.incident_bucket_arn}/*"] }
      ],
      length(local.bedrock_model_arns) > 0 ? [
        { Effect = "Allow", Action = ["bedrock:InvokeModel"], Resource = local.bedrock_model_arns }
      ] : [],
      length(local.llm_secret_arns) > 0 ? [
        { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = local.llm_secret_arns }
      ] : [],
    )
  })
}

resource "aws_iam_role_policy" "action_planner_inline" {
  name = "${var.project_name}-action-planner-inline"
  role = aws_iam_role.action_planner.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        { Effect = "Allow", Action = ["s3:GetObject"], Resource = ["${var.incident_bucket_arn}/*"] }
      ],
      length(local.bedrock_model_arns) > 0 ? [
        { Effect = "Allow", Action = ["bedrock:InvokeModel"], Resource = local.bedrock_model_arns }
      ] : [],
      length(concat(local.github_secret_arns, local.llm_secret_arns)) > 0 ? [
        { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = concat(local.github_secret_arns, local.llm_secret_arns) }
      ] : [],
    )
  })
}

resource "aws_iam_role_policy" "confidence_scorer_inline" {
  name = "${var.project_name}-confidence-scorer-inline"
  role = aws_iam_role.confidence_scorer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = ["${var.incident_bucket_arn}/*"] }
    ]
  })
}

# ── Step Functions: invoke all agent Lambdas ──────────────────────────────────
resource "aws_iam_role_policy" "sfn_invoke_lambdas" {
  name = "${var.project_name}-sfn-invoke-lambdas"
  role = aws_iam_role.sfn.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = ["*"] },
      { Effect = "Allow", Action = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"], Resource = ["*"] },
      { Effect = "Allow", Action = [
        "logs:CreateLogDelivery",
        "logs:GetLogDelivery",
        "logs:UpdateLogDelivery",
        "logs:DeleteLogDelivery",
        "logs:ListLogDeliveries",
        "logs:PutResourcePolicy",
        "logs:DescribeResourcePolicies",
        "logs:DescribeLogGroups",
      ], Resource = ["*"] }
    ]
  })
}

# ── EventBridge: start Step Functions executions ─────────────────────────────
resource "aws_iam_role_policy" "events_start_sfn" {
  name = "${var.project_name}-events-start-sfn"
  role = aws_iam_role.events_sfn.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["states:StartExecution"], Resource = ["*"] }
    ]
  })
}

# ── Outputs ───────────────────────────────────────────────────────────────────
output "signal_collector_role_arn" { value = aws_iam_role.signal_collector.arn }
output "decision_engine_role_arn" { value = aws_iam_role.decision_engine.arn }
output "outcome_validator_role_arn" { value = aws_iam_role.outcome_validator.arn }
output "classifier_agent_role_arn" { value = aws_iam_role.classifier_agent.arn }
output "root_cause_agent_role_arn" { value = aws_iam_role.root_cause_agent.arn }
output "action_planner_role_arn" { value = aws_iam_role.action_planner.arn }
output "confidence_scorer_role_arn" { value = aws_iam_role.confidence_scorer.arn }
output "sfn_role_arn" { value = aws_iam_role.sfn.arn }
output "events_sfn_role_arn" { value = aws_iam_role.events_sfn.arn }
