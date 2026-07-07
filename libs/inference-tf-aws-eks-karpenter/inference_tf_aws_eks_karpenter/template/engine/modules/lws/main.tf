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
}
