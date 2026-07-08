variable "kro_namespace" {
  description = "Namespace to install the KRO controller."
  type        = string
}

variable "kro_version" {
  description = "Version of the KRO Helm chart."
  type        = string
}

variable "platform_node_selector" {
  description = "Node selector to pin the controller to platform nodes."
  type        = map(string)
}
