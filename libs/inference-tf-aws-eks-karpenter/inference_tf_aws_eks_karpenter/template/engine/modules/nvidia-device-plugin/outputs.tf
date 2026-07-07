output "nvidia_plugin_namespace" {
  description = "Namespace where NVIDIA device plugin is installed."
  value       = helm_release.nvidia_device_plugin.namespace
}

output "nvidia_plugin_version" {
  description = "Installed NVIDIA device plugin chart version."
  value       = helm_release.nvidia_device_plugin.version
}
