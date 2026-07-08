resource "helm_release" "kro" {
  name             = "kro"
  namespace        = var.kro_namespace
  create_namespace = true
  repository       = "oci://ghcr.io/kubernetes-sigs/kro"
  chart            = "kro"
  version          = var.kro_version

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
