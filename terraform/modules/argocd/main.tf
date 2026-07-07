variable "project_name" { type = string }
variable "github_owner" { type = string }
variable "github_repo" { type = string }
variable "gitops_repo_revision" {
  type    = string
  default = "main"
}
variable "bootstrap_applications" {
  type    = bool
  default = true
}

locals {
  repo_url = "https://github.com/${var.github_owner}/${var.github_repo}.git"

  applications = {
    "platform-policies" = {
      destination_namespace = "argocd"
      path                  = "gitops/policies"
      sync_options          = ["SkipDryRunOnMissingResource=true"]
    }
    "demo-staging" = {
      destination_namespace = "demo-staging"
      path                  = "gitops/clusters/staging"
      sync_options = [
        "CreateNamespace=true",
        "SkipDryRunOnMissingResource=true",
      ]
    }
    "demo-prod" = {
      destination_namespace = "demo-prod"
      path                  = "gitops/clusters/prod"
      sync_options = [
        "CreateNamespace=true",
        "SkipDryRunOnMissingResource=true",
      ]
    }
  }

  extra_objects = [
    for name, app in local.applications : {
      apiVersion = "argoproj.io/v1alpha1"
      kind       = "Application"
      metadata = {
        name      = name
        namespace = "argocd"
      }
      spec = {
        project = "default"
        source = {
          repoURL        = local.repo_url
          targetRevision = var.gitops_repo_revision
          path           = app.path
        }
        destination = {
          server    = "https://kubernetes.default.svc"
          namespace = app.destination_namespace
        }
        syncPolicy = {
          automated = {
            prune    = true
            selfHeal = true
          }
          syncOptions = app.sync_options
        }
      }
    }
  ]
}

resource "helm_release" "argocd" {
  name             = "argo-cd"
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-cd"
  version          = "9.5.15"
  namespace        = "argocd"
  create_namespace = true
}

# The Application CRs cannot ship as extraObjects of the argo-cd release:
# Helm validates the whole manifest before the Argo CRDs exist. Applying them
# as a second release makes the CRDs available at validation time.
resource "helm_release" "bootstrap_applications" {
  count = var.bootstrap_applications ? 1 : 0

  name       = "argo-cd-applications"
  repository = "https://bedag.github.io/helm-charts"
  chart      = "raw"
  version    = "2.0.0"
  namespace  = "argocd"

  values = [
    yamlencode({
      resources = local.extra_objects
    })
  ]

  depends_on = [helm_release.argocd]
}
