resource "helm_release" "kro" {
  name             = "kro"
  namespace        = var.kro_namespace
  create_namespace = true
  repository       = "oci://ghcr.io/kubernetes-sigs/kro"
  chart            = "kro"
  version          = var.kro_version
}
