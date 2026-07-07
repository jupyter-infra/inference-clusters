resource "helm_release" "efa_device_plugin" {
  name             = "aws-efa-k8s-device-plugin"
  namespace        = var.efa_namespace
  create_namespace = true
  repository       = "https://aws.github.io/eks-charts"
  chart            = "aws-efa-k8s-device-plugin"
  version          = var.efa_plugin_version

  set {
    name  = "tolerations[0].key"
    value = "nvidia.com/gpu"
  }
  set {
    name  = "tolerations[0].operator"
    value = "Exists"
  }
  set {
    name  = "tolerations[0].effect"
    value = "NoSchedule"
  }

  set {
    name  = "nodeSelector.inference/efa"
    value = "true"
  }
}
