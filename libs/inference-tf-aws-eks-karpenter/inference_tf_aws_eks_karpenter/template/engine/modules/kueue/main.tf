resource "helm_release" "kueue" {
  name             = "kueue"
  namespace        = var.kueue_namespace
  create_namespace = true
  repository       = "https://kubernetes-sigs.github.io/kueue"
  chart            = "kueue"
  version          = var.kueue_version

  # LWS integration (requires LWS CRD installed separately)
  set {
    name  = "enableLeaderWorkerSet"
    value = "true"
  }

  # Prometheus metrics (optional — requires kube-prometheus stack)
  set {
    name  = "controller.metrics.serviceMonitor.enabled"
    value = var.enable_prometheus_metrics ? "true" : "false"
  }

  # TopologyAwareScheduling feature gate (alpha/beta — not on by default)
  dynamic "set" {
    for_each = var.enable_topology_aware_scheduling ? [1] : []
    content {
      name  = "controller.featureGates.TopologyAwareScheduling"
      value = "true"
    }
  }
}

resource "kubernetes_manifest" "gpu_resource_flavor" {
  manifest = {
    apiVersion = "kueue.x-k8s.io/v1beta1"
    kind       = "ResourceFlavor"
    metadata = {
      name = "gpu-multinode"
    }
    spec = {
      nodeLabels = {
        "inference/node-type" = "multinode-gpu"
      }
      tolerations = [{
        key      = "nvidia.com/gpu"
        operator = "Exists"
        effect   = "NoSchedule"
      }]
    }
  }

  depends_on = [helm_release.kueue]
}

resource "kubernetes_manifest" "gpu_cluster_queue" {
  manifest = {
    apiVersion = "kueue.x-k8s.io/v1beta1"
    kind       = "ClusterQueue"
    metadata = {
      name = var.cluster_queue_name
    }
    spec = {
      cohort = var.cohort_name
      resourceGroups = [{
        coveredResources = ["cpu", "memory", "nvidia.com/gpu"]
        flavors = [{
          name = "gpu-multinode"
          resources = [
            {
              name         = "nvidia.com/gpu"
              nominalQuota = var.gpu_quota
              lendingLimit = var.gpu_lending_limit
            },
            {
              name         = "cpu"
              nominalQuota = var.cpu_quota
            },
            {
              name         = "memory"
              nominalQuota = var.memory_quota
            },
          ]
        }]
      }]
      preemption = {
        withinClusterQueue  = "LowerPriority"
        reclaimWithinCohort = "LowerPriority"
      }
    }
  }

  depends_on = [kubernetes_manifest.gpu_resource_flavor]
}

resource "kubernetes_manifest" "gpu_local_queue" {
  manifest = {
    apiVersion = "kueue.x-k8s.io/v1beta1"
    kind       = "LocalQueue"
    metadata = {
      name      = "inference"
      namespace = var.workload_namespace
    }
    spec = {
      clusterQueue = var.cluster_queue_name
    }
  }

  depends_on = [kubernetes_manifest.gpu_cluster_queue]
}

# Topology resource for TAS (optional — only when feature gate is enabled)
resource "kubernetes_manifest" "topology" {
  count = var.enable_topology_aware_scheduling ? 1 : 0

  manifest = {
    apiVersion = "kueue.x-k8s.io/v1beta1"
    kind       = "Topology"
    metadata = {
      name = "gpu-topology"
    }
    spec = {
      levels = [for level in var.topology_levels : { nodeLabel = level }]
    }
  }

  depends_on = [helm_release.kueue]
}
