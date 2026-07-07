resource "helm_release" "efs_csi_driver" {
  name             = "aws-efs-csi-driver"
  namespace        = var.efs_namespace
  create_namespace = true
  repository       = "https://kubernetes-sigs.github.io/aws-efs-csi-driver"
  chart            = "aws-efs-csi-driver"
  version          = var.efs_csi_version

  set {
    name  = "controller.serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
    value = var.efs_csi_role_arn
  }

  set {
    name  = "node.tolerations[0].key"
    value = "nvidia.com/gpu"
  }
  set {
    name  = "node.tolerations[0].operator"
    value = "Exists"
  }
  set {
    name  = "node.tolerations[0].effect"
    value = "NoSchedule"
  }
}

resource "kubernetes_storage_class" "efs" {
  metadata {
    name = "efs-sc"
  }
  storage_provisioner = "efs.csi.aws.com"
  parameters = {
    provisioningMode = "efs-ap"
    fileSystemId     = var.efs_filesystem_id
    directoryPerms   = "700"
  }
  mount_options = ["tls"]
}

resource "kubernetes_persistent_volume" "model_weights" {
  count = var.efs_filesystem_id != "" ? 1 : 0

  metadata {
    name = "model-weights-pv"
  }
  spec {
    capacity = {
      storage = var.model_weights_storage_size
    }
    volume_mode                      = "Filesystem"
    access_modes                     = ["ReadOnlyMany"]
    persistent_volume_reclaim_policy = "Retain"
    storage_class_name               = "efs-sc"
    persistent_volume_source {
      csi {
        driver        = "efs.csi.aws.com"
        volume_handle = var.efs_filesystem_id
      }
    }
  }

  depends_on = [kubernetes_storage_class.efs]
}
