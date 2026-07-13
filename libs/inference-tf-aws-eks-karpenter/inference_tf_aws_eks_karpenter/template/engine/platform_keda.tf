# === KEDA — pod autoscaling ===
#
# Completes the loop Karpenter can't: Karpenter scales NODES on Pending pods, but
# something must scale PODS first. KEDA scales pods on a Prometheus query (GPU util
# via DCGM, requests-in-flight, queue depth) or SQS depth — the right signal for GPU
# inference (built-in HPA only sees CPU/mem). The chain the POC measures:
#   DCGM/vLLM metric -> Prometheus -> KEDA (pod scale) -> Pending -> Karpenter (node scale)
#
# Placement: all three control-plane pods (operator, metrics-apiserver, admission
# webhook) on the tainted system NG — they watch/scale; they never run on GPU
# nodes. The chart's global nodeSelector/tolerations fan out to all three components.
#
# Images: KEDA publishes its three images ONLY to ghcr.io;
# all three are VENDORED to our ECR via CodeBuild (images.tf)
# and referenced by their <registry>/<repository>:<tag> split.

locals {
  keda_namespace = "keda"
}

resource "helm_release" "keda" {
  name             = "keda"
  repository       = "https://kedacore.github.io/charts"
  chart            = "keda"
  version          = var.keda_chart_version
  namespace        = local.keda_namespace
  create_namespace = true

  # Image repins + placement are too nested for flat `set` entries (each component's
  # image is a registry/repository/tag triple), so pass one values doc.
  values = [yamlencode({
    # Two replicas each for the leader-elected operator + metrics-apiserver so a
    # failover (system-NG node drain) keeps a warm standby. Both are single-active
    # (only the leader serves), so this buys availability, not throughput. The
    # admission webhooks are stateless (not leader-elected), so they stay at the
    # chart default.
    operator      = { replicaCount = 2 }
    metricsServer = { replicaCount = 2 }

    # All three components vendored to ECR; the chart builds "<registry>/<repository>:<tag>".
    image = {
      keda = {
        registry   = local.ecr_registry
        repository = aws_ecr_repository.vendored["keda_operator"].name
        tag        = local.vendored_tag
      }
      metricsApiServer = {
        registry   = local.ecr_registry
        repository = aws_ecr_repository.vendored["keda_metrics_apiserver"].name
        tag        = local.vendored_tag
      }
      webhooks = {
        registry   = local.ecr_registry
        repository = aws_ecr_repository.vendored["keda_admission_webhooks"].name
        tag        = local.vendored_tag
      }
    }

    # Global placement — the chart falls back to these for every component when a
    # per-component nodeSelector/tolerations isn't set (verified in the 2.20 templates).
    nodeSelector = local.system_node_selector
    tolerations  = [local.system_toleration]
  })]

  depends_on = [
    null_resource.cluster_addons,
    null_resource.pullthrough_ready,
    module.node_group,
    # KEDA scales on Prometheus queries — the metrics stack must exist first (and its
    # ServiceMonitor CRD, which KEDA's own ServiceMonitor and consumer objects use).
    helm_release.kube_prometheus_stack,
    # All three images are vendored to ECR — they must land before the pods pull.
    null_resource.image_vendor,
  ]
}
