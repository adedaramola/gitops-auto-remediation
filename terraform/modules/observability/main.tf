variable "project_name" { type = string }
variable "aws_region" { type = string }
variable "oidc_provider_arn" { type = string }
variable "oidc_provider" {
  type        = string
  description = "OIDC issuer URL of the EKS cluster, without the https:// prefix."
}
variable "webhook_url" {
  type        = string
  default     = ""
  description = "API Gateway URL that Alertmanager posts to. Leave empty to skip wiring the receiver."
}
variable "webhook_secret" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Shared secret. Sent as Authorization: Bearer <secret>; signal_collector also accepts x-webhook-secret."
}
variable "prometheus_node_port" {
  type        = number
  default     = 30090
  description = "Fixed NodePort used for the private Prometheus load balancer path."
}

locals {
  grafana_service_account = "kube-prometheus-stack-grafana"
  monitoring_namespace    = "monitoring"
}

########################
# Grafana CloudWatch IRSA — lets Grafana query CloudWatch Logs/Metrics
########################
data "aws_iam_policy_document" "grafana_assume_role" {
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
      values   = ["system:serviceaccount:${local.monitoring_namespace}:${local.grafana_service_account}"]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "grafana" {
  name               = "${var.project_name}-grafana"
  assume_role_policy = data.aws_iam_policy_document.grafana_assume_role.json
  tags               = { Project = var.project_name }
}

resource "aws_iam_role_policy" "grafana_cloudwatch" {
  name = "${var.project_name}-grafana-cloudwatch"
  role = aws_iam_role.grafana.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "cloudwatch:DescribeAlarmsForMetric",
          "cloudwatch:DescribeAlarmHistory",
          "cloudwatch:DescribeAlarms",
          "cloudwatch:ListMetrics",
          "cloudwatch:GetMetricData",
          "cloudwatch:GetMetricStatistics",
          "cloudwatch:GetInsightRuleReport",
          "logs:DescribeLogGroups",
          "logs:GetLogGroupFields",
          "logs:StartQuery",
          "logs:StopQuery",
          "logs:GetQueryResults",
          "logs:GetLogEvents",
          "ec2:DescribeTags",
          "ec2:DescribeInstances",
          "ec2:DescribeRegions",
          "tag:GetResources",
        ]
        Resource = "*"
      },
    ]
  })
}

########################
# kube-prometheus-stack — Prometheus + Grafana + Alertmanager
########################
locals {
  # When webhook_url is empty we skip the alertmanager block entirely so the
  # chart's default config (null receiver) stays in effect.
  alertmanager_values = var.webhook_url == "" ? {} : {
    alertmanager = {
      config = {
        global = {
          resolve_timeout = "5m"
        }
        route = {
          receiver        = "gitops-sentinel"
          group_by        = ["alertname", "service"]
          group_wait      = "30s"
          group_interval  = "5m"
          repeat_interval = "4h"
        }
        receivers = [
          {
            name = "null"
          },
          {
            name = "gitops-sentinel"
            webhook_configs = [{
              url           = var.webhook_url
              send_resolved = false
              http_config = {
                authorization = {
                  type        = "Bearer"
                  credentials = var.webhook_secret
                }
              }
            }]
          }
        ]
      }
    }
  }

  prometheus_rules = {
    groups = [{
      name = "gitops-sentinel"
      rules = [
        {
          alert = "PodCrashLooping"
          expr  = "rate(kube_pod_container_status_restarts_total[5m]) * 60 * 5 > 2"
          for   = "5m"
          labels = {
            severity = "critical"
          }
          annotations = {
            summary     = "Pod {{ $labels.pod }} is crash-looping"
            description = "Pod {{ $labels.pod }} in {{ $labels.namespace }} restarted more than 2 times in 5 minutes."
          }
        },
        {
          alert = "PodOOMKilled"
          expr  = "kube_pod_container_status_last_terminated_reason{reason=\"OOMKilled\"} == 1"
          for   = "1m"
          labels = {
            severity = "critical"
          }
          annotations = {
            summary     = "Pod {{ $labels.pod }} was OOMKilled"
            description = "Pod {{ $labels.pod }} in {{ $labels.namespace }} was terminated by the OOM killer."
          }
        },
        {
          alert = "DeploymentReplicaDrift"
          expr  = "kube_deployment_status_replicas_available != kube_deployment_spec_replicas"
          for   = "10m"
          labels = {
            severity = "warning"
          }
          annotations = {
            summary     = "Deployment {{ $labels.deployment }} has replica drift"
            description = "Deployment {{ $labels.deployment }} in {{ $labels.namespace }} has had unavailable replicas for 10 minutes."
          }
        },
        {
          alert = "HighHTTP5xxErrorRate"
          expr  = "sum by (service) (rate(http_requests_total{status=~\"5..\"}[5m])) > 0.5"
          for   = "5m"
          labels = {
            severity = "critical"
          }
          annotations = {
            summary     = "Service {{ $labels.service }} has high 5xx error rate"
            description = "Service {{ $labels.service }} is returning 5xx responses at more than 0.5 req/s."
          }
        },
      ]
    }]
  }
}

resource "helm_release" "observability" {
  name             = "kube-prometheus-stack"
  repository       = "https://prometheus-community.github.io/helm-charts"
  chart            = "kube-prometheus-stack"
  namespace        = local.monitoring_namespace
  create_namespace = true

  values = [yamlencode(merge(local.alertmanager_values, {
    additionalPrometheusRulesMap = {
      gitops-sentinel = local.prometheus_rules
    }

    grafana = {
      serviceAccount = {
        create = true
        name   = local.grafana_service_account
        annotations = {
          "eks.amazonaws.com/role-arn" = aws_iam_role.grafana.arn
        }
      }

      additionalDataSources = [{
        name      = "CloudWatch"
        type      = "cloudwatch"
        access    = "proxy"
        isDefault = false
        jsonData = {
          authType      = "default"
          defaultRegion = var.aws_region
        }
      }]
    }

    prometheus = {
      service = {
        type     = "NodePort"
        nodePort = var.prometheus_node_port
      }
    }
  }))]

  depends_on = [aws_iam_role_policy.grafana_cloudwatch]
}

output "grafana_role_arn" {
  value = aws_iam_role.grafana.arn
}
