resource "helm_release" "scheduler_plugins" {
  count = var.gang_scheduling_provider == "coscheduling" ? 1 : 0

  name             = "scheduler-plugins"
  namespace        = var.gang_scheduling_namespace
  create_namespace = true
  repository       = "https://kubernetes-sigs.github.io/scheduler-plugins"
  chart            = "scheduler-plugins"
  version          = var.coscheduling_version

  set {
    name  = "plugins.enabled"
    value = "Coscheduling"
  }
}

resource "helm_release" "volcano" {
  count = var.gang_scheduling_provider == "volcano" ? 1 : 0

  name             = "volcano"
  namespace        = var.gang_scheduling_namespace
  create_namespace = true
  repository       = "https://volcano-sh.github.io/helm-charts"
  chart            = "volcano"
  version          = var.volcano_version

  set {
    name  = "custom.scheduler_enable"
    value = "true"
  }

  set {
    name  = "custom.controller_enable"
    value = "true"
  }
}

resource "kubernetes_manifest" "pod_group_crd" {
  count = var.gang_scheduling_provider == "coscheduling" ? 1 : 0

  manifest = {
    apiVersion = "apiextensions.k8s.io/v1"
    kind       = "CustomResourceDefinition"
    metadata = {
      name = "podgroups.scheduling.x-k8s.io"
    }
    spec = {
      group = "scheduling.x-k8s.io"
      names = {
        kind     = "PodGroup"
        plural   = "podgroups"
        singular = "podgroup"
      }
      scope = "Namespaced"
      versions = [{
        name    = "v1alpha1"
        served  = true
        storage = true
        schema = {
          openAPIV3Schema = {
            type = "object"
            properties = {
              spec = {
                type = "object"
                properties = {
                  minMember = {
                    type = "integer"
                  }
                  scheduleTimeoutSeconds = {
                    type = "integer"
                  }
                }
              }
            }
          }
        }
      }]
    }
  }
}
