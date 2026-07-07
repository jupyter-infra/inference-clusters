resource "helm_release" "nvidia_device_plugin" {
  name             = "nvidia-device-plugin"
  namespace        = var.nvidia_namespace
  create_namespace = true
  repository       = "https://nvidia.github.io/k8s-device-plugin"
  chart            = "nvidia-device-plugin"
  version          = var.nvidia_plugin_version

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
    name  = "gfd.enabled"
    value = "true"
  }

  set {
    name  = "nfd.enabled"
    value = var.enable_nfd ? "true" : "false"
  }
}
