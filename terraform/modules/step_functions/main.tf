variable "project_name" { type = string }
variable "triage_lambda_arn" { type = string }
variable "diagnosis_lambda_arn" { type = string }
variable "remediation_lambda_arn" { type = string }
variable "risk_lambda_arn" { type = string }
variable "agent_lambda_arn" { type = string } # decision_engine Lambda used for PR opening
variable "sfn_role_arn" { type = string }
variable "log_retention_days" {
  type    = number
  default = 30
}
variable "kms_key_arn" {
  type    = string
  default = null
}

resource "aws_cloudwatch_log_group" "sfn" {
  name              = "/aws/vendedlogs/states/${var.project_name}-multi-agent-pipeline"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
  tags              = { Project = var.project_name }
}

resource "aws_sfn_state_machine" "multi_agent" {
  name     = "${var.project_name}-multi-agent-pipeline"
  role_arn = var.sfn_role_arn
  type     = "STANDARD"

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.sfn.arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }

  definition = jsonencode({
    Comment = "AI-Powered Multi-Agent GitOps Remediation Pipeline"
    StartAt = "TriageIncident"

    States = {
      TriageIncident = {
        Type       = "Task"
        Resource   = var.triage_lambda_arn
        ResultPath = "$.triage"
        Next       = "DiagnoseIncident"
        Retry = [{
          ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException"]
          IntervalSeconds = 2
          MaxAttempts     = 2
          BackoffRate     = 2.0
        }]
        Catch = [{
          ErrorEquals = ["States.ALL"]
          ResultPath  = "$.triage_error"
          Next        = "TriageFallback"
        }]
      }

      TriageFallback = {
        Type = "Pass"
        Result = {
          severity_class = "medium"
          incident_type  = "Unknown"
          blast_radius   = "contained"
          priority       = 3
          key_signals    = ["triage_agent_unavailable"]
        }
        ResultPath = "$.triage"
        Next       = "DiagnoseIncident"
      }

      DiagnoseIncident = {
        Type       = "Task"
        Resource   = var.diagnosis_lambda_arn
        ResultPath = "$.diagnosis"
        Next       = "ProposeRemediation"
        Retry = [{
          ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException"]
          IntervalSeconds = 2
          MaxAttempts     = 2
          BackoffRate     = 2.0
        }]
        Catch = [{
          ErrorEquals = ["States.ALL"]
          ResultPath  = "$.diagnosis_error"
          Next        = "DiagnosisFallback"
        }]
      }

      DiagnosisFallback = {
        Type = "Pass"
        Result = {
          root_cause           = "Unknown — diagnosis agent unavailable"
          contributing_factors = ["diagnosis_agent_unavailable"]
          affected_components  = []
          diagnosis_confidence = 20
        }
        ResultPath = "$.diagnosis"
        Next       = "ProposeRemediation"
      }

      ProposeRemediation = {
        Type       = "Task"
        Resource   = var.remediation_lambda_arn
        ResultPath = "$.remediation"
        Next       = "AssessRisk"
        Retry = [{
          ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException"]
          IntervalSeconds = 2
          MaxAttempts     = 2
          BackoffRate     = 2.0
        }]
        Catch = [{
          ErrorEquals = ["States.ALL"]
          ResultPath  = "$.remediation_error"
          Next        = "RemediationFallback"
        }]
      }

      RemediationFallback = {
        Type = "Pass"
        Parameters = {
          action = "restart_rollout"
          params = {}
          "target" = {
            "service.$" = "$.service"
            "env.$"     = "$.env"
          }
          reasoning    = "Heuristic fallback: restart_rollout is the safest default action."
          alternatives = []
        }
        ResultPath = "$.remediation"
        Next       = "AssessRisk"
      }

      AssessRisk = {
        Type       = "Task"
        Resource   = var.risk_lambda_arn
        ResultPath = "$.risk"
        Next       = "RouteByConfidence"
        Retry = [{
          ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException"]
          IntervalSeconds = 2
          MaxAttempts     = 2
          BackoffRate     = 2.0
        }]
        Catch = [{
          ErrorEquals = ["States.ALL"]
          ResultPath  = "$.risk_error"
          Next        = "OpenRemediationPR" # safe default on risk agent failure
        }]
      }

      RouteByConfidence = {
        Type = "Choice"
        Choices = [
          {
            Variable     = "$.risk.recommendation"
            StringEquals = "auto_apply"
            Next         = "QueueForAutoApply"
          },
          {
            Variable     = "$.risk.recommendation"
            StringEquals = "escalate"
            Next         = "EscalateToHuman"
          }
        ]
        Default = "OpenRemediationPR"
      }

      # auto_apply: high-confidence path — opens and immediately merges the PR
      QueueForAutoApply = {
        Type       = "Task"
        Resource   = var.agent_lambda_arn
        Comment    = "High-confidence path: opens PR and merges without human review."
        ResultPath = "$.pr_result"
        Next       = "PipelineComplete"
      }

      OpenRemediationPR = {
        Type       = "Task"
        Resource   = var.agent_lambda_arn
        Comment    = "Opens a GitHub PR for human review. Used for medium-confidence remediations."
        ResultPath = "$.pr_result"
        Next       = "PipelineComplete"
      }

      EscalateToHuman = {
        Type       = "Pass"
        Comment    = "Low confidence — no automated action. Slack notification sent by risk_agent."
        Result     = { escalated = true, reason = "confidence_too_low" }
        ResultPath = "$.escalation"
        Next       = "PipelineComplete"
      }

      PipelineComplete = {
        Type = "Succeed"
      }
    }
  })

  tags = { Project = var.project_name }
}

output "state_machine_arn" { value = aws_sfn_state_machine.multi_agent.arn }
output "state_machine_name" { value = aws_sfn_state_machine.multi_agent.name }
output "log_group_name" { value = aws_cloudwatch_log_group.sfn.name }
output "log_group_arn" { value = aws_cloudwatch_log_group.sfn.arn }
