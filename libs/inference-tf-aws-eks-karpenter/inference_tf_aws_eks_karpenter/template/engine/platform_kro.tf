# === KRO — resource orchestration ===
#
# KRO (kro.run) expands ONE custom resource (a ResourceGroup instance) into a whole
# graph — Deployment + Service + KEDA ScaledObject + ServiceMonitor + PVC — behind a
# simple schema. The template ships starter ResourceGroups (charts/kro) that encode the
# consumer contract as code; a workload owner instantiates one with a few
# fields and KRO expands the standardized graph.
#
# Placement: the single controller pod on the tainted system NG — it
# watches/orchestrates; the workload replicas it manages land on Karpenter GPU nodes.
#
# Images/chart: both the chart (oci://registry.k8s.io/kro/charts/kro) and the
# controller image (registry.k8s.io/kro/kro) live on registry.k8s.io — a no-creds
# pull-through upstream — so both reach us via pull-through, no vendoring.

locals {
  kro_namespace = "kro"
}

resource "helm_release" "kro" {
  name             = "kro"
  repository       = "oci://registry.k8s.io/kro/charts"
  chart            = "kro"
  version          = var.kro_chart_version
  namespace        = local.kro_namespace
  create_namespace = true

  # NO chart-pull auth: registry.k8s.io serves the KRO chart anonymously, same as the
  # Karpenter chart — a minted token would reintroduce the perpetual-diff /
  # stale-token footgun for zero benefit.

  set = [
    # Repin the controller image to its pull-through URI (PRIMARY resolution):
    # registry.k8s.io/kro/kro -> <registry>/registry-k8s/kro/kro. Tag-only (no digest)
    # so pull-through import-on-miss fires. Tag is the chart version prefixed with "v".
    {
      name  = "image.repository"
      value = "${local.ecr_registry}/registry-k8s/kro/kro"
    },
    { name = "image.tag", value = "v${var.kro_chart_version}" },
    # Two replicas so a leader failover (system-NG node drain) keeps a warm standby;
    # KRO is leader-elected (enableLeaderElection defaults true), so one is active.
    { name = "deployment.replicaCount", value = "2" },
    # System NG placement. The chart REPLACES nodeSelector wholesale (no fallback
    # merge), so carry the chart's own kubernetes.io/os=linux alongside our selector.
    { name = "deployment.nodeSelector.kubernetes\\.io/os", value = "linux" },
    { name = "deployment.nodeSelector.inference/role", value = "system" },
    { name = "deployment.tolerations[0].key", value = "inference/role" },
    { name = "deployment.tolerations[0].operator", value = "Equal" },
    { name = "deployment.tolerations[0].value", value = "system" },
    { name = "deployment.tolerations[0].effect", value = "NoSchedule" },
  ]

  depends_on = [
    null_resource.cluster_addons,
    null_resource.pullthrough_ready,
    module.node_group,
  ]
}

# --- Starter ResourceGroups (charts/kro) ---
#
# First-party local chart: the starter RGD(s) encoding the consumer contract, installed
# atomically like charts/karpenter and charts/metrics. Requires the KRO CRDs (installed
# by the controller release above) and KEDA's ScaledObject CRD (the starter graph emits
# one), so it is ordered after both operators.
resource "helm_release" "kro_starters" {
  name      = "kro-starters"
  chart     = "${path.module}/../charts/kro"
  namespace = local.kro_namespace

  set = [
    { name = "karpenterGpuNodeSelector", value = "nvidia-g" },
    { name = "serviceMonitorLabel", value = "kube-prometheus-stack" },
    # Chart content hash so editing a chart file triggers a re-apply (see main.tf).
    { name = "chartContentHash", value = local.chart_hashes["kro"] },
  ]

  depends_on = [
    helm_release.kro,
    helm_release.keda,
  ]
}
