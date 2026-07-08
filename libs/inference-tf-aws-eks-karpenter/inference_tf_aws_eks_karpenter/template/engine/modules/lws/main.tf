resource "helm_release" "leader_worker_set" {
  name             = "leader-worker-set"
  namespace        = var.lws_namespace
  create_namespace = true
  repository       = "https://kubernetes-sigs.github.io/leader-worker-set"
  chart            = "leader-worker-set"
  version          = var.lws_version

  set {
    name  = "replicaCount"
    value = "1"
  }

  # Pin controller to platform nodes (not GPU dataplane nodes)
  dynamic "set" {
    for_each = var.platform_node_selector
    content {
      name  = "nodeSelector.${replace(set.key, "/", "\\.")}"
      value = set.value
    }
  }

  set {
    name  = "tolerations[0].key"
    value = "CriticalAddonsOnly"
  }
  set {
    name  = "tolerations[0].operator"
    value = "Exists"
  }
}
