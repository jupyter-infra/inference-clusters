variable "efa_namespace" {
  description = "Namespace to install the EFA device plugin DaemonSet."
  type        = string
}

variable "efa_plugin_version" {
  description = "Version of the aws-efa-k8s-device-plugin Helm chart."
  type        = string
}
