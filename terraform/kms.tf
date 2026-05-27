data "aws_caller_identity" "current" {}

# KMS key for CloudWatch log group encryption.
# Only created when var.enable_log_encryption = true.
resource "aws_kms_key" "logs" {
  count = var.enable_log_encryption ? 1 : 0

  description             = "${local.name} CloudWatch log encryption"
  deletion_window_in_days = 14
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowRootAccountFullAccess"
        Effect = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "AllowCloudWatchLogs"
        Effect = "Allow"
        Principal = { Service = "logs.${var.aws_region}.amazonaws.com" }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey",
        ]
        Resource = "*"
        Condition = {
          ArnEquals = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"
          }
        }
      },
    ]
  })

  tags = { Project = local.name }
}

resource "aws_kms_alias" "logs" {
  count = var.enable_log_encryption ? 1 : 0

  name          = "alias/${local.name}-logs"
  target_key_id = aws_kms_key.logs[0].key_id
}

# Expose for use in other modules and log groups
locals {
  kms_log_key_arn = var.enable_log_encryption ? aws_kms_key.logs[0].arn : null
}
