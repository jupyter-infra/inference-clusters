# === Shared workload namespace ===
#
# The namespace where inference workloads run — user Deployments, KRO serving graphs,
# the Kueue LocalQueue, PVCs. Owned by the ENGINE and UNGATED so it exists regardless of
# which optional operators (Kueue/LWS/...) are enabled, and so toggling one off never
# deletes the namespace or the workloads inside it.
#
# Destroy ordering (mirrors the eks-oidc kubernetes_namespace_v1.shared pattern): every
# release/resource that ships objects INTO this namespace lists it in depends_on, so on
# destroy it is torn down AFTER them (nothing is left fighting to recreate objects in a
# terminating namespace). Its own depends_on anchors on the admin access associations —
# the K8s provider's authorization — and the node group, so the namespace delete happens
# BEFORE auth/nodes go away; otherwise the delete would hang "forbidden" and, for any
# PVCs in the namespace, the CSI controllers (kept alive by the same anchors) can still
# detach the volumes.
resource "kubernetes_namespace_v1" "workload" {
  metadata {
    name = var.workload_namespace
    labels = {
      "app.kubernetes.io/managed-by" = "jupyter-deploy"
    }
  }

  depends_on = [
    aws_eks_access_policy_association.admin_role,
    aws_eks_access_policy_association.admin_user,
    module.node_group,
  ]
}
