variable "lws_namespace" {
  description = "Namespace to install the LeaderWorkerSet controller."
  type        = string
}

variable "lws_version" {
  description = "Version of the LeaderWorkerSet Helm chart."
  type        = string
}

variable "platform_node_selector" {
  description = "Node selector to pin the controller to platform nodes."
  type        = map(string)
}
