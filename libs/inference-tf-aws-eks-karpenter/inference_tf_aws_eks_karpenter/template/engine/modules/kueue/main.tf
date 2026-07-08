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

  # TopologyAwareScheduling — required for EFA co-location guarantees
  set {
    name  = "controller.featureGates.TopologyAwareScheduling"
    value = "true"
  }

  # Prometheus ServiceMonitor — required for queue health visibility
  set {
    name  = "controller.metrics.serviceMonitor.enabled"
    value = "true"
  }

  # waitForPodsReady — required to prevent silent resource leaks on
  # partial provisioning failure (3/4 nodes arrive, 4th never does)
  set {
    name  = "controller.waitForPodsReady.enable"
    value = "true"
  }
  set {
    name  = "controller.waitForPodsReady.timeout"
    value = var.wait_for_pods_ready_timeout
  }
  set {
    name  = "controller.waitForPodsReady.requeuingStrategy.timestamp"
    value = "Creation"
  }
  set {
    name  = "controller.waitForPodsReady.requeuingStrategy.backoffLimitCount"
    value = tostring(var.wait_for_pods_ready_retries)
  }

  # Pin controller to platform nodes (not GPU dataplane nodes)
  dynamic "set" {
    for_each = var.platform_node_selector
    content {
      name  = "controller.nodeSelector.${replace(set.key, "/", "\\.")}"
      value = set.value
    }
  }

  set {
    name  = "controller.tolerations[0].key"
    value = "CriticalAddonsOnly"
  }
  set {
    name  = "controller.tolerations[0].operator"
    value = "Exists"
  }
}

# Topology resource — defines the data center hierarchy for TAS.
# EFA requires all nodes in the same AZ; TAS enforces this at admission time.
resource "kubernetes_manifest" "topology" {
  manifest = {
    apiVersion = "kueue.x-k8s.io/v1alpha1"
    kind       = "Topology"
    metadata = {
      name = "default"
    }
    spec = {
      levels = [for level in var.topology_levels : { nodeLabel = level }]
    }
  }

  depends_on = [helm_release.kueue]
}

resource "kubernetes_manifest" "gpu_resource_flavor" {
  manifest = {
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "ResourceFlavor"
    metadata = {
      name = "gpu-multinode"
    }
    spec = {
      nodeLabels = {
        "inference/node-type" = "multinode-gpu"
      }
      topologyName = "default"
      tolerations = [{
        key      = "nvidia.com/gpu"
        operator = "Exists"
        effect   = "NoSchedule"
      }]
    }
  }

  depends_on = [kubernetes_manifest.topology]
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
