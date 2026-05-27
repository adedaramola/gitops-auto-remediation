variable "project_name" { type = string }

########################
# kube-prometheus-stack (Prometheus + Grafana + Alertmanager)
########################
resource "helm_release" "observability" {
  name             = "kube-prometheus-stack"
  repository       = "https://prometheus-community.github.io/helm-charts"
  chart            = "kube-prometheus-stack"
  namespace        = "monitoring"
  create_namespace = true
}
