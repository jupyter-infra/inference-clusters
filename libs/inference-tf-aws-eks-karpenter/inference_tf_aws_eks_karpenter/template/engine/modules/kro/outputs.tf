output "kro_namespace" {
  description = "Namespace where KRO controller is installed."
  value       = helm_release.kro.namespace
}

output "kro_version" {
  description = "Installed KRO chart version."
  value       = helm_release.kro.version
}
