# Operational guardrails for the auto-apply merge path.

# Kill switch: flip to "false" to stop the Decision Engine from auto-merging
# PRs (it will open them for human review instead). Takes effect within ~30s.
#   aws ssm put-parameter --name /gitops-sentinel/auto-apply-enabled \
#     --value false --overwrite
# Terraform ignores value drift so an operator flip is never reverted by apply.
resource "aws_ssm_parameter" "auto_apply_enabled" {
  name        = "/${local.name}/auto-apply-enabled"
  description = "Auto-apply kill switch: 'true' allows the Decision Engine to merge high-confidence PRs."
  type        = "String"
  value       = "true"

  lifecycle {
    ignore_changes = [value]
  }

  tags = { Project = local.name }
}

output "auto_apply_kill_switch" {
  value       = aws_ssm_parameter.auto_apply_enabled.name
  description = "SSM parameter name — set to 'false' to disable auto-apply merges."
}
