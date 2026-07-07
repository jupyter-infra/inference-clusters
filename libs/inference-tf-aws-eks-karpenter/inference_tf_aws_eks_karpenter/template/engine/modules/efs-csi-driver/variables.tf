variable "efs_namespace" {
  description = "Namespace to install the EFS CSI driver."
  type        = string
}

variable "efs_csi_version" {
  description = "Version of the aws-efs-csi-driver Helm chart."
  type        = string
}

variable "efs_csi_role_arn" {
  description = "IAM role ARN for the EFS CSI driver service account (IRSA)."
  type        = string
}

variable "efs_filesystem_id" {
  description = "EFS filesystem ID for model weight storage. Leave empty to skip PV creation."
  type        = string
}

variable "model_weights_storage_size" {
  description = "Storage size for the model weights PV (e.g. '2Ti')."
  type        = string
}
