output "efa_plugin_namespace" {
  description = "Namespace where EFA device plugin is installed."
  value       = helm_release.efa_device_plugin.namespace
}

output "efa_plugin_version" {
  description = "Installed EFA device plugin chart version."
  value       = helm_release.efa_device_plugin.version
}
