output "efs_csi_namespace" {
  description = "Namespace where EFS CSI driver is installed."
  value       = helm_release.efs_csi_driver.namespace
}

output "efs_storage_class_name" {
  description = "Name of the EFS StorageClass."
  value       = kubernetes_storage_class.efs.metadata[0].name
}

output "model_weights_pv_name" {
  description = "Name of the model weights PersistentVolume (empty if not created)."
  value       = var.efs_filesystem_id != "" ? kubernetes_persistent_volume.model_weights[0].metadata[0].name : ""
}
