variable "project_name" { type = string }
variable "cluster_name" { type = string }
variable "aws_region" { type = string }
variable "oidc_provider_arn" { type = string }
variable "oidc_provider" {
  type        = string
  description = "OIDC issuer URL of the EKS cluster, without the https:// prefix (e.g. oidc.eks.us-east-1.amazonaws.com/id/ABC...)"
}
variable "log_retention_days" {
  type    = number
  default = 30
}
variable "kms_key_arn" {
  type    = string
  default = null
}

locals {
  namespace            = "amazon-cloudwatch"
  service_account_name = "aws-for-fluent-bit"
  log_group_name       = "/aws/eks/${var.cluster_name}/pod-logs"
}

resource "aws_cloudwatch_log_group" "pod_logs" {
  name              = local.log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
  tags              = { Project = var.project_name }
}

data "aws_iam_policy_document" "fluent_bit_assume_role" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    effect  = "Allow"

    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider}:sub"
      values   = ["system:serviceaccount:${local.namespace}:${local.service_account_name}"]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "fluent_bit" {
  name               = "${var.project_name}-fluent-bit"
  assume_role_policy = data.aws_iam_policy_document.fluent_bit_assume_role.json
  tags               = { Project = var.project_name }
}

resource "aws_iam_role_policy" "fluent_bit_logs" {
  name = "${var.project_name}-fluent-bit-logs"
  role = aws_iam_role.fluent_bit.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:CreateLogGroup",
          "logs:DescribeLogStreams",
          "logs:PutLogEvents",
          "logs:PutRetentionPolicy",
        ]
        Resource = ["${aws_cloudwatch_log_group.pod_logs.arn}:*"]
      },
    ]
  })
}

resource "helm_release" "fluent_bit" {
  name             = "aws-for-fluent-bit"
  repository       = "https://aws.github.io/eks-charts"
  chart            = "aws-for-fluent-bit"
  namespace        = local.namespace
  create_namespace = true

  values = [yamlencode({
    serviceAccount = {
      create = true
      name   = local.service_account_name
      annotations = {
        "eks.amazonaws.com/role-arn" = aws_iam_role.fluent_bit.arn
      }
    }

    cloudWatchLogs = {
      enabled          = true
      region           = var.aws_region
      logGroupName     = aws_cloudwatch_log_group.pod_logs.name
      logStreamPrefix  = "fluentbit-"
      autoCreateGroup  = false
      logRetentionDays = var.log_retention_days
    }

    # Disable other outputs (Kinesis, Firehose, OpenSearch) — CloudWatch only.
    firehose = {
      enabled = false
    }
    kinesis = {
      enabled = false
    }
    elasticsearch = {
      enabled = false
    }

    # Include K8s metadata (pod name, namespace, labels, annotations) for correlation.
    input = {
      tag          = "kube.*"
      memBufLimit  = "5MB"
      readFromHead = "Off"
    }

    filter = {
      mergeLog          = "On"
      mergeLogKey       = "log_processed"
      keepLog           = "Off"
      k8sLoggingParser  = "On"
      k8sLoggingExclude = "Off"
    }

    tolerations = [{
      operator = "Exists"
    }]
  })]

  depends_on = [aws_iam_role_policy.fluent_bit_logs]
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.pod_logs.name
}

output "log_group_arn" {
  value = aws_cloudwatch_log_group.pod_logs.arn
}

output "role_arn" {
  value = aws_iam_role.fluent_bit.arn
}
