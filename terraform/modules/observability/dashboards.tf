# Pipeline-health Grafana dashboards delivered as ConfigMaps.
# The kube-prometheus-stack sidecar watches for the grafana_dashboard=1 label
# and auto-provisions any JSON in these ConfigMaps into Grafana.

locals {
  dashboard_namespace = local.monitoring_namespace

  pipeline_dashboard = jsonencode({
    title       = "GitOps Sentinel — Pipeline Health"
    uid         = "gitops-sentinel-pipeline"
    schemaVersion = 36
    refresh     = "30s"
    time        = { from = "now-3h", to = "now" }

    # ── Row: Incident intake ───────────────────────────────────────────────────
    panels = [
      {
        id    = 1
        type  = "stat"
        title = "Incidents Received (1h)"
        gridPos = { h = 4, w = 4, x = 0, y = 0 }
        datasource = { type = "cloudwatch", uid = "cloudwatch" }
        targets = [{
          namespace  = "GitOpsSentinel"
          metricName = "IncidentsReceived"
          statistic  = "Sum"
          period     = "3600"
          dimensions = {}
        }]
        options = { reduceOptions = { calcs = ["sum"] } }
      },
      {
        id    = 2
        type  = "stat"
        title = "Dedup Suppressions (1h)"
        gridPos = { h = 4, w = 4, x = 4, y = 0 }
        datasource = { type = "cloudwatch", uid = "cloudwatch" }
        targets = [{
          namespace  = "GitOpsSentinel"
          metricName = "IncidentsDeduplicated"
          statistic  = "Sum"
          period     = "3600"
          dimensions = {}
        }]
        options = { reduceOptions = { calcs = ["sum"] } }
      },
      {
        id    = 3
        type  = "stat"
        title = "PRs Opened (1h)"
        gridPos = { h = 4, w = 4, x = 8, y = 0 }
        datasource = { type = "cloudwatch", uid = "cloudwatch" }
        targets = [{
          namespace  = "GitOpsSentinel"
          metricName = "PRsOpened"
          statistic  = "Sum"
          period     = "3600"
          dimensions = {}
        }]
        options = { reduceOptions = { calcs = ["sum"] } }
      },
      {
        id    = 4
        type  = "stat"
        title = "Outcomes Validated (1h)"
        gridPos = { h = 4, w = 4, x = 12, y = 0 }
        datasource = { type = "cloudwatch", uid = "cloudwatch" }
        targets = [{
          namespace  = "GitOpsSentinel"
          metricName = "OutcomeValidated"
          statistic  = "Sum"
          period     = "3600"
          dimensions = {}
        }]
        fieldConfig = { defaults = { color = { mode = "fixed", fixedColor = "green" } } }
        options = { reduceOptions = { calcs = ["sum"] } }
      },
      {
        id    = 5
        type  = "stat"
        title = "Outcome Failures (1h)"
        gridPos = { h = 4, w = 4, x = 16, y = 0 }
        datasource = { type = "cloudwatch", uid = "cloudwatch" }
        targets = [{
          namespace  = "GitOpsSentinel"
          metricName = "OutcomeFailed"
          statistic  = "Sum"
          period     = "3600"
          dimensions = {}
        }]
        fieldConfig = { defaults = { color = { mode = "fixed", fixedColor = "red" } } }
        options = { reduceOptions = { calcs = ["sum"] } }
      },
      {
        id    = 6
        type  = "stat"
        title = "Auto-Reverts (1h)"
        gridPos = { h = 4, w = 4, x = 20, y = 0 }
        datasource = { type = "cloudwatch", uid = "cloudwatch" }
        targets = [{
          namespace  = "GitOpsSentinel"
          metricName = "RevertPROpened"
          statistic  = "Sum"
          period     = "3600"
          dimensions = {}
        }]
        fieldConfig = { defaults = { color = { mode = "fixed", fixedColor = "orange" } } }
        options = { reduceOptions = { calcs = ["sum"] } }
      },

      # ── Row: Confidence routing ─────────────────────────────────────────────
      {
        id    = 7
        type  = "piechart"
        title = "Routing Decisions (24h)"
        gridPos = { h = 8, w = 8, x = 0, y = 4 }
        datasource = { type = "cloudwatch", uid = "cloudwatch" }
        targets = [
          {
            alias      = "auto_apply"
            namespace  = "GitOpsSentinel"
            metricName = "RoutingDecision"
            statistic  = "Sum"
            period     = "86400"
            dimensions = { Decision = "auto_apply" }
          },
          {
            alias      = "open_pr"
            namespace  = "GitOpsSentinel"
            metricName = "RoutingDecision"
            statistic  = "Sum"
            period     = "86400"
            dimensions = { Decision = "open_pr" }
          },
          {
            alias      = "escalate"
            namespace  = "GitOpsSentinel"
            metricName = "RoutingDecision"
            statistic  = "Sum"
            period     = "86400"
            dimensions = { Decision = "escalate" }
          },
        ]
      },
      {
        id    = 8
        type  = "timeseries"
        title = "Confidence Score (raw)"
        gridPos = { h = 8, w = 16, x = 8, y = 4 }
        datasource = { type = "cloudwatch", uid = "cloudwatch" }
        targets = [{
          namespace  = "GitOpsSentinel"
          metricName = "ConfidenceScore"
          statistic  = "Average"
          period     = "300"
          dimensions = {}
        }]
        fieldConfig = { defaults = { unit = "none", min = 0, max = 100 } }
      },

      # ── Row: Lambda errors (Logs Insights) ─────────────────────────────────
      {
        id    = 9
        type  = "timeseries"
        title = "Lambda ERROR logs (5m buckets)"
        gridPos = { h = 8, w = 12, x = 0, y = 12 }
        datasource = { type = "cloudwatch", uid = "cloudwatch" }
        targets = [{
          queryMode     = "Logs"
          logGroupNames = [
            "/aws/lambda/gitops-sentinel-signal-collector",
            "/aws/lambda/gitops-sentinel-decision-engine",
            "/aws/lambda/gitops-sentinel-outcome-validator",
            "/aws/lambda/gitops-sentinel-classifier-agent",
            "/aws/lambda/gitops-sentinel-root-cause-agent",
            "/aws/lambda/gitops-sentinel-action-planner",
            "/aws/lambda/gitops-sentinel-confidence-scorer",
          ]
          expression = "fields @timestamp, component, msg | filter level = 'ERROR' | stats count(*) as errors by bin(5m), component | sort @timestamp desc"
        }]
      },
      {
        id    = 10
        type  = "timeseries"
        title = "Step Functions failures (5m buckets)"
        gridPos = { h = 8, w = 12, x = 12, y = 12 }
        datasource = { type = "cloudwatch", uid = "cloudwatch" }
        targets = [{
          queryMode     = "Logs"
          logGroupNames = ["/aws/vendedlogs/states/gitops-sentinel-multi-agent-pipeline"]
          expression    = "fields @timestamp | filter type in ['ExecutionFailed','TaskFailed'] | stats count(*) as failures by bin(5m) | sort @timestamp desc"
        }]
      },

      # ── Row: EventBridge DLQ depth ──────────────────────────────────────────
      {
        id    = 11
        type  = "stat"
        title = "EventBridge DLQ depth (current)"
        gridPos = { h = 4, w = 6, x = 0, y = 20 }
        datasource = { type = "cloudwatch", uid = "cloudwatch" }
        targets = [{
          namespace  = "AWS/SQS"
          metricName = "ApproximateNumberOfMessagesVisible"
          statistic  = "Maximum"
          period     = "60"
          dimensions = { QueueName = "gitops-sentinel-eventbridge-dlq" }
        }]
        fieldConfig = {
          defaults = {
            thresholds = {
              steps = [
                { color = "green", value = 0 },
                { color = "red", value = 1 },
              ]
            }
            color = { mode = "thresholds" }
          }
        }
      },
    ]
  })
}

resource "kubernetes_config_map_v1" "pipeline_dashboard" {
  metadata {
    name      = "gitops-sentinel-pipeline-dashboard"
    namespace = local.dashboard_namespace
    labels = {
      grafana_dashboard = "1"
    }
  }

  data = {
    "pipeline-health.json" = local.pipeline_dashboard
  }

  depends_on = [helm_release.observability]
}
