variable "nvidia_namespace" {
  description = "Namespace to install the NVIDIA device plugin."
  type        = string
}

variable "nvidia_plugin_version" {
  description = "Version of the nvidia-device-plugin Helm chart."
  type        = string
}

variable "enable_nfd" {
  description = "Enable Node Feature Discovery alongside the GPU plugin."
  type        = bool
}
