output "lws_namespace" {
  description = "Namespace where LeaderWorkerSet controller is installed."
  value       = helm_release.leader_worker_set.namespace
}

output "lws_version" {
  description = "Installed LeaderWorkerSet chart version."
  value       = helm_release.leader_worker_set.version
}
