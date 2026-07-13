# === LeaderWorkerSet — multi-node pod group lifecycle ===
#
# LWS manages leader+worker pod groups with coordinated lifecycle
# (RecreateGroupOnPodRestart). Required for multi-node inference where NCCL
# process groups are not recoverable — if one pod dies, all must restart.
#
# Placement: the single controller pod on the tainted system NG — it watches
# LWS CRs and manages pod templates; the actual inference pods it manages
# land on Karpenter GPU nodes.
#
# Images/chart: published to registry.k8s.io (no-creds pull-through).

locals {
  lws_namespace = "lws-system"
}

resource "helm_release" "leader_worker_set" {
  count = var.enable_lws ? 1 : 0

  name             = "lws"
  repository       = "oci://registry.k8s.io/lws/charts"
  chart            = "lws"
  version          = var.lws_chart_version
  namespace        = local.lws_namespace
  create_namespace = true

  set = [
    # Two replicas so a leader failover (system-NG node drain) keeps a warm standby;
    # the LWS controller is leader-elected, so only one is active at a time.
    { name = "replicaCount", value = "2" },
    # Repin the controller image to its pull-through URI (PRIMARY resolution):
    # registry.k8s.io/lws/lws -> <registry>/registry-k8s/lws/lws.
    { name = "image.manager.repository", value = "${local.ecr_registry}/registry-k8s/lws/lws" },
    { name = "image.manager.tag", value = "v${var.lws_chart_version}" },
    # System NG placement.
    { name = "nodeSelector.inference/role", value = "system" },
    { name = "tolerations[0].key", value = "inference/role" },
    { name = "tolerations[0].operator", value = "Equal" },
    { name = "tolerations[0].value", value = "system" },
    { name = "tolerations[0].effect", value = "NoSchedule" },
  ]

  depends_on = [
    null_resource.cluster_addons,
    null_resource.pullthrough_ready,
    module.node_group,
  ]
}
