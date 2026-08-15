# Root module wires all components together.

locals {
  name                           = var.project_name
  prometheus_proxy_group         = "gitops-sentinel-prometheus-readers"
  prometheus_private_node_port   = 30090
  prometheus_query_url_effective = var.enable_private_prometheus_endpoint ? "http://${aws_lb.prometheus_internal[0].dns_name}:9090" : var.prometheus_query_url
  enable_prometheus_k8s_proxy    = can(regex("(^https?://)?[^./]+\\.[^./]+\\.svc(\\.|$)", var.prometheus_query_url))
  enable_cluster_api_read_access = var.enable_k8s_readonly_enrichment || local.enable_prometheus_k8s_proxy
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
  cluster_version = "1.34"

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  enable_cluster_creator_admin_permissions = true
  cluster_endpoint_public_access           = true

  create_kms_key            = false
  cluster_encryption_config = {}

  cluster_enabled_log_types              = ["api", "audit", "authenticator", "controllerManager", "scheduler"]
  cloudwatch_log_group_retention_in_days = var.audit_log_retention_days
  cloudwatch_log_group_kms_key_id        = local.kms_log_key_arn

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

# Fresh EKS clusters can report ACTIVE before the API server consistently accepts
# the creator token. This wait makes Helm/Kubernetes resources much less likely
# to race cluster access propagation on clean demo deployments.
resource "time_sleep" "kubernetes_api_ready" {
  depends_on      = [module.eks]
  create_duration = var.kubernetes_api_ready_wait
}

########################
# GitOps & Observability (Helm)
########################
module "argocd" {
  source                 = "./modules/argocd"
  github_owner           = var.github_owner
  github_repo            = var.github_repo
  gitops_repo_revision   = var.gitops_repo_revision
  bootstrap_applications = var.bootstrap_argocd_applications

  depends_on = [time_sleep.kubernetes_api_ready]
}

module "observability" {
  source               = "./modules/observability"
  project_name         = local.name
  aws_region           = var.aws_region
  oidc_provider_arn    = module.eks.oidc_provider_arn
  oidc_provider        = replace(module.eks.cluster_oidc_issuer_url, "https://", "")
  webhook_url          = var.enable_api_gateway ? module.webhook[0].webhook_url : ""
  webhook_secret       = var.webhook_secret
  alarm_actions        = local.alarm_action_arns
  prometheus_node_port = local.prometheus_private_node_port

  depends_on = [aws_cloudwatch_log_group.lambda_logs, time_sleep.kubernetes_api_ready]
}

module "log_shipping" {
  source             = "./modules/log_shipping"
  project_name       = local.name
  cluster_name       = var.cluster_name
  aws_region         = var.aws_region
  oidc_provider_arn  = module.eks.oidc_provider_arn
  oidc_provider      = replace(module.eks.cluster_oidc_issuer_url, "https://", "")
  log_retention_days = var.log_retention_days
  kms_key_arn        = local.kms_log_key_arn

  depends_on = [time_sleep.kubernetes_api_ready]
}

module "gatekeeper" {
  source = "./modules/gatekeeper"

  depends_on = [time_sleep.kubernetes_api_ready]
}

resource "aws_security_group" "telemetry_lambdas" {
  count = var.enable_private_prometheus_endpoint ? 1 : 0

  name        = "${local.name}-telemetry-lambdas"
  description = "Telemetry Lambdas that query private Prometheus over the VPC."
  vpc_id      = module.vpc.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Project = local.name
  }
}

resource "aws_security_group_rule" "prometheus_nodeport_from_vpc" {
  count = var.enable_private_prometheus_endpoint ? 1 : 0

  type              = "ingress"
  from_port         = local.prometheus_private_node_port
  to_port           = local.prometheus_private_node_port
  protocol          = "tcp"
  security_group_id = module.eks.node_security_group_id
  cidr_blocks       = [var.vpc_cidr]
}

resource "aws_security_group_rule" "telemetry_lambdas_to_cluster_api" {
  count = var.enable_private_prometheus_endpoint ? 1 : 0

  type                     = "ingress"
  from_port                = 443
  to_port                  = 443
  protocol                 = "tcp"
  security_group_id        = module.eks.cluster_primary_security_group_id
  source_security_group_id = aws_security_group.telemetry_lambdas[0].id
}

resource "aws_lb" "prometheus_internal" {
  count = var.enable_private_prometheus_endpoint ? 1 : 0

  name               = "gitops-sentinel-prom"
  internal           = true
  load_balancer_type = "network"
  subnets            = module.vpc.private_subnets

  tags = {
    Project = local.name
  }
}

resource "aws_lb_target_group" "prometheus_internal" {
  count = var.enable_private_prometheus_endpoint ? 1 : 0

  name        = "gitops-sentinel-prom"
  port        = local.prometheus_private_node_port
  protocol    = "TCP"
  target_type = "instance"
  vpc_id      = module.vpc.vpc_id

  health_check {
    protocol = "TCP"
    port     = local.prometheus_private_node_port
  }
}

resource "aws_lb_listener" "prometheus_internal" {
  count = var.enable_private_prometheus_endpoint ? 1 : 0

  load_balancer_arn = aws_lb.prometheus_internal[0].arn
  port              = 9090
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.prometheus_internal[0].arn
  }
}

resource "aws_autoscaling_attachment" "prometheus_internal" {
  count = var.enable_private_prometheus_endpoint ? 1 : 0

  autoscaling_group_name = module.eks.eks_managed_node_groups["default"].node_group_autoscaling_group_names[0]
  lb_target_group_arn    = aws_lb_target_group.prometheus_internal[0].arn
}

# Allow telemetry Lambdas to authenticate to the EKS API when either:
# - incident enrichment is enabled, or
# - Prometheus is reached through the in-cluster service proxy path.
resource "aws_eks_access_entry" "signal_collector" {
  count = local.enable_cluster_api_read_access ? 1 : 0

  cluster_name      = module.eks.cluster_name
  principal_arn     = module.iam.signal_collector_role_arn
  kubernetes_groups = [local.prometheus_proxy_group]
  type              = "STANDARD"
}

resource "aws_eks_access_policy_association" "signal_collector_view" {
  count = local.enable_cluster_api_read_access ? 1 : 0

  cluster_name  = module.eks.cluster_name
  principal_arn = module.iam.signal_collector_role_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_access_entry.signal_collector]
}

resource "aws_eks_access_entry" "outcome_validator" {
  count = local.enable_cluster_api_read_access ? 1 : 0

  cluster_name      = module.eks.cluster_name
  principal_arn     = module.iam.outcome_validator_role_arn
  kubernetes_groups = [local.prometheus_proxy_group]
  type              = "STANDARD"
}

resource "aws_eks_access_policy_association" "outcome_validator_view" {
  count = local.enable_cluster_api_read_access ? 1 : 0

  cluster_name  = module.eks.cluster_name
  principal_arn = module.iam.outcome_validator_role_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_access_entry.outcome_validator]
}

resource "kubernetes_role_v1" "prometheus_proxy_reader" {
  count = local.enable_cluster_api_read_access ? 1 : 0

  metadata {
    name      = "gitops-sentinel-prometheus-proxy-reader"
    namespace = "monitoring"
  }

  rule {
    api_groups = [""]
    resources  = ["services", "endpoints", "pods"]
    verbs      = ["get", "list", "watch"]
  }

  rule {
    api_groups = [""]
    resources  = ["services/proxy", "pods/proxy"]
    verbs      = ["get"]
  }

  depends_on = [time_sleep.kubernetes_api_ready, module.observability]
}

resource "kubernetes_role_binding_v1" "prometheus_proxy_reader" {
  count = local.enable_cluster_api_read_access ? 1 : 0

  metadata {
    name      = "gitops-sentinel-prometheus-proxy-reader"
    namespace = "monitoring"
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.prometheus_proxy_reader[0].metadata[0].name
  }

  subject {
    kind      = "Group"
    name      = local.prometheus_proxy_group
    api_group = "rbac.authorization.k8s.io"
  }

  depends_on = [time_sleep.kubernetes_api_ready, module.observability, kubernetes_role_v1.prometheus_proxy_reader]
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
  source                  = "./modules/iam"
  project_name            = local.name
  aws_region              = var.aws_region
  model_provider          = var.model_provider
  bedrock_model_id        = var.bedrock_model_id
  cluster_arn             = module.eks.cluster_arn
  incident_bucket_arn     = module.signals_bucket.bucket_arn
  event_bus_arn           = module.eventing.event_bus_arn
  incidents_table_arn     = module.signals_table.table_arn
  audit_table_arn         = module.audit_log.table_arn
  auto_apply_param_arn    = aws_ssm_parameter.auto_apply_enabled.arn
  github_token_secret_arn = var.github_token_secret_arn
  openai_secret_arn       = var.openai_secret_arn
}

module "signal_collector_lambda" {
  source                         = "./modules/lambda_signal_collector"
  project_name                   = local.name
  incident_bucket_name           = module.signals_bucket.bucket_name
  event_bus_name                 = module.eventing.event_bus_name
  role_arn                       = module.iam.signal_collector_role_arn
  cluster_name                   = var.cluster_name
  prometheus_query_url           = local.prometheus_query_url_effective
  enable_k8s_readonly_enrichment = var.enable_k8s_readonly_enrichment
  incidents_table_name           = module.signals_table.table_name
  webhook_secret                 = var.webhook_secret
  enable_multi_agent             = var.enable_multi_agent
  audit_table_name               = module.audit_log.table_name
  subnet_ids                     = var.enable_private_prometheus_endpoint ? module.vpc.private_subnets : []
  security_group_ids             = var.enable_private_prometheus_endpoint ? [aws_security_group.telemetry_lambdas[0].id] : []
}

module "decision_engine_lambda" {
  source                  = "./modules/lambda_decision_engine"
  project_name            = local.name
  role_arn                = module.iam.decision_engine_role_arn
  event_bus_name          = module.eventing.event_bus_name
  github_owner            = var.github_owner
  github_repo             = var.github_repo
  github_token_secret_arn = var.github_token_secret_arn
  openai_secret_arn       = var.openai_secret_arn
  model_provider          = var.model_provider
  bedrock_model_id        = var.bedrock_model_id
  cluster_name            = var.cluster_name
  prometheus_query_url    = var.prometheus_query_url
  audit_table_name        = module.audit_log.table_name

  auto_apply_enabled_param = aws_ssm_parameter.auto_apply_enabled.name
  auto_apply_max_per_hour  = var.auto_apply_max_per_hour
}

module "outcome_validator_lambda" {
  source                  = "./modules/lambda_outcome_validator"
  project_name            = local.name
  role_arn                = module.iam.outcome_validator_role_arn
  cluster_name            = var.cluster_name
  prometheus_query_url    = local.prometheus_query_url_effective
  slack_webhook_url       = var.slack_webhook_url
  event_bus_name          = module.eventing.event_bus_name
  github_owner            = var.github_owner
  github_repo             = var.github_repo
  github_token_secret_arn = var.github_token_secret_arn
  audit_table_name        = module.audit_log.table_name
  subnet_ids              = var.enable_private_prometheus_endpoint ? module.vpc.private_subnets : []
  security_group_ids      = var.enable_private_prometheus_endpoint ? [aws_security_group.telemetry_lambdas[0].id] : []
}

########################
# Multi-agent Lambda functions
########################
module "classifier_agent" {
  source            = "./modules/lambda_classifier_agent"
  project_name      = local.name
  role_arn          = module.iam.classifier_agent_role_arn
  aws_region        = var.aws_region
  model_provider    = var.model_provider
  bedrock_model_id  = var.bedrock_model_id
  openai_secret_arn = var.openai_secret_arn
}

module "root_cause_agent" {
  source            = "./modules/lambda_root_cause_agent"
  project_name      = local.name
  role_arn          = module.iam.root_cause_agent_role_arn
  aws_region        = var.aws_region
  model_provider    = var.model_provider
  bedrock_model_id  = var.bedrock_model_id
  openai_secret_arn = var.openai_secret_arn
}

module "action_planner" {
  source                  = "./modules/lambda_action_planner"
  project_name            = local.name
  role_arn                = module.iam.action_planner_role_arn
  aws_region              = var.aws_region
  model_provider          = var.model_provider
  bedrock_model_id        = var.bedrock_model_id
  github_owner            = var.github_owner
  github_repo             = var.github_repo
  github_token_secret_arn = var.github_token_secret_arn
  openai_secret_arn       = var.openai_secret_arn
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
  kms_key_arn            = local.kms_log_key_arn
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
  kms_key_arn         = local.kms_log_key_arn
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
  kms_key_id        = local.kms_log_key_arn

  tags = { Project = local.name }
}
