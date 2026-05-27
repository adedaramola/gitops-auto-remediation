# Root module wires all components together.

locals {
  name = var.project_name
}

########################
# VPC + EKS (deploy-ready)
########################
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  name = "${local.name}-vpc"
  cidr = var.vpc_cidr

  azs             = slice(data.aws_availability_zones.available.names, 0, var.az_count)
  private_subnets = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 4, i)]
  public_subnets  = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 4, i + 8)]

  enable_nat_gateway = true
  single_nat_gateway = true

  tags = {
    Project = local.name
  }
}

data "aws_availability_zones" "available" {}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = var.cluster_name
  cluster_version = "1.33"

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  enable_cluster_creator_admin_permissions = true
  cluster_endpoint_public_access           = true

  create_kms_key            = false
  cluster_encryption_config = {}

  cluster_enabled_log_types              = ["api", "audit", "authenticator", "controllerManager", "scheduler"]
  cloudwatch_log_group_retention_in_days = var.log_retention_days

  eks_managed_node_groups = {
    default = {
      instance_types = ["t2.medium"]
      min_size       = 2
      max_size       = 2
      desired_size   = 2
    }
  }

  tags = {
    Project = local.name
  }
}

########################
# GitOps & Observability (Helm)
########################
module "argocd" {
  source       = "./modules/argocd"
  project_name = local.name
}

module "observability" {
  source            = "./modules/observability"
  project_name      = local.name
  cluster_name      = var.cluster_name
  aws_region        = var.aws_region
  oidc_provider_arn = module.eks.oidc_provider_arn
  oidc_provider     = replace(module.eks.cluster_oidc_issuer_url, "https://", "")
  webhook_url       = var.enable_api_gateway ? module.webhook[0].webhook_url : ""
  webhook_secret    = var.webhook_secret
  alarm_actions     = var.alarm_actions
}

module "log_shipping" {
  source             = "./modules/log_shipping"
  project_name       = local.name
  cluster_name       = var.cluster_name
  aws_region         = var.aws_region
  oidc_provider_arn  = module.eks.oidc_provider_arn
  oidc_provider      = replace(module.eks.cluster_oidc_issuer_url, "https://", "")
  log_retention_days = var.log_retention_days
}

module "gatekeeper" {
  source       = "./modules/gatekeeper"
  project_name = local.name
}

########################
# Eventing + Signal store
########################
module "eventing" {
  source       = "./modules/eventbridge"
  project_name = local.name
}

module "signals_bucket" {
  source       = "./modules/s3_incidents"
  project_name = local.name
}

module "iam" {
  source              = "./modules/iam"
  project_name        = local.name
  incident_bucket_arn = module.signals_bucket.bucket_arn
  event_bus_arn       = module.eventing.event_bus_arn
}

module "signal_collector_lambda" {
  source                         = "./modules/lambda_signal_collector"
  project_name                   = local.name
  incident_bucket_name           = module.signals_bucket.bucket_name
  event_bus_name                 = module.eventing.event_bus_name
  role_arn                       = module.iam.signal_collector_role_arn
  aws_region                     = var.aws_region
  cluster_name                   = var.cluster_name
  prometheus_query_url           = var.prometheus_query_url
  enable_k8s_readonly_enrichment = var.enable_k8s_readonly_enrichment
  incidents_table_name           = module.signals_table.table_name
  webhook_secret                 = var.webhook_secret
  enable_multi_agent             = var.enable_multi_agent
  audit_table_name               = module.audit_log.table_name
}

module "decision_engine_lambda" {
  source                  = "./modules/lambda_decision_engine"
  project_name            = local.name
  role_arn                = module.iam.decision_engine_role_arn
  github_owner            = var.github_owner
  github_repo             = var.github_repo
  github_token_secret_arn = var.github_token_secret_arn
  model_provider          = var.model_provider
  aws_region              = var.aws_region
  cluster_name            = var.cluster_name
  prometheus_query_url    = var.prometheus_query_url
  audit_table_name        = module.audit_log.table_name
}

module "outcome_validator_lambda" {
  source                  = "./modules/lambda_outcome_validator"
  project_name            = local.name
  role_arn                = module.iam.outcome_validator_role_arn
  aws_region              = var.aws_region
  cluster_name            = var.cluster_name
  prometheus_query_url    = var.prometheus_query_url
  slack_webhook_url       = var.slack_webhook_url
  event_bus_name          = module.eventing.event_bus_name
  github_owner            = var.github_owner
  github_repo             = var.github_repo
  github_token_secret_arn = var.github_token_secret_arn
  audit_table_name        = module.audit_log.table_name
}

########################
# Multi-agent Lambda functions
########################
module "classifier_agent" {
  source         = "./modules/lambda_classifier_agent"
  project_name   = local.name
  role_arn       = module.iam.classifier_agent_role_arn
  aws_region     = var.aws_region
  model_provider = var.model_provider
}

module "root_cause_agent" {
  source         = "./modules/lambda_root_cause_agent"
  project_name   = local.name
  role_arn       = module.iam.root_cause_agent_role_arn
  aws_region     = var.aws_region
  model_provider = var.model_provider
}

module "action_planner" {
  source                  = "./modules/lambda_action_planner"
  project_name            = local.name
  role_arn                = module.iam.action_planner_role_arn
  aws_region              = var.aws_region
  model_provider          = var.model_provider
  github_owner            = var.github_owner
  github_repo             = var.github_repo
  github_token_secret_arn = var.github_token_secret_arn
}

module "confidence_scorer_agent" {
  source       = "./modules/lambda_confidence_scorer"
  project_name = local.name
  role_arn     = module.iam.confidence_scorer_role_arn
  aws_region   = var.aws_region
}

########################
# Step Functions — sentinel pipeline
########################
module "sentinel_pipeline" {
  source                 = "./modules/step_functions"
  project_name           = local.name
  triage_lambda_arn      = module.classifier_agent.lambda_arn
  diagnosis_lambda_arn   = module.root_cause_agent.lambda_arn
  remediation_lambda_arn = module.action_planner.lambda_arn
  risk_lambda_arn        = module.confidence_scorer_agent.lambda_arn
  agent_lambda_arn       = module.decision_engine_lambda.lambda_arn
  sfn_role_arn           = module.iam.sfn_role_arn
  log_retention_days     = var.log_retention_days
}

########################
# EventBridge rules
########################
module "rules" {
  source                       = "./modules/eventbridge_rules"
  project_name                 = local.name
  event_bus_name               = module.eventing.event_bus_name
  bundler_lambda_arn           = module.signal_collector_lambda.lambda_arn
  agent_lambda_arn             = module.decision_engine_lambda.lambda_arn
  outcome_validator_lambda_arn = module.outcome_validator_lambda.lambda_arn
  sfn_arn                      = module.sentinel_pipeline.state_machine_arn
  events_sfn_role_arn          = module.iam.events_sfn_role_arn
}

########################
# Permissions: allow EventBridge -> Lambda invoke
########################
resource "aws_lambda_permission" "eventbridge_invoke_signal_collector" {
  statement_id  = "AllowExecutionFromEventBridgeSignalCollector"
  action        = "lambda:InvokeFunction"
  function_name = module.signal_collector_lambda.lambda_name
  principal     = "events.amazonaws.com"
  source_arn    = module.rules.alert_in_rule_arn
}

resource "aws_lambda_permission" "eventbridge_invoke_decision_engine" {
  statement_id  = "AllowExecutionFromEventBridgeDecisionEngine"
  action        = "lambda:InvokeFunction"
  function_name = module.decision_engine_lambda.lambda_name
  principal     = "events.amazonaws.com"
  source_arn    = module.rules.bundle_created_rule_arn
}

resource "aws_lambda_permission" "eventbridge_invoke_outcome_validator" {
  statement_id  = "AllowExecutionFromEventBridgeOutcomeValidator"
  action        = "lambda:InvokeFunction"
  function_name = module.outcome_validator_lambda.lambda_name
  principal     = "events.amazonaws.com"
  source_arn    = module.rules.verify_rule_arn
}

########################
# API Gateway (Webhook intake) -> Signal Collector Lambda
########################
module "webhook" {
  source              = "./modules/apigw_webhook"
  count               = var.enable_api_gateway ? 1 : 0
  project_name        = local.name
  bundler_lambda_arn  = module.signal_collector_lambda.lambda_arn
  bundler_lambda_name = module.signal_collector_lambda.lambda_name
  log_retention_days  = var.log_retention_days
}

output "webhook_url" {
  value       = var.enable_api_gateway ? module.webhook[0].webhook_url : null
  description = "Send Alertmanager webhooks here (POST)."
}


module "signals_table" {
  source       = "./modules/dynamodb_incidents"
  project_name = local.name
}

module "audit_log" {
  source       = "./modules/dynamodb_audit_log"
  project_name = local.name
}

# ── Lambda log groups with retention ─────────────────────────────────────────
locals {
  lambda_function_names = [
    "${local.name}-signal-collector",
    "${local.name}-decision-engine",
    "${local.name}-outcome-validator",
    "${local.name}-classifier-agent",
    "${local.name}-root-cause-agent",
    "${local.name}-action-planner",
    "${local.name}-confidence-scorer",
  ]
}

resource "aws_cloudwatch_log_group" "lambda_logs" {
  for_each          = toset(local.lambda_function_names)
  name              = "/aws/lambda/${each.value}"
  retention_in_days = var.log_retention_days

  tags = { Project = local.name }
}
