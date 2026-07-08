output "kueue_namespace" {
  description = "Namespace where Kueue controller is installed."
  value       = helm_release.kueue.namespace
}

output "cluster_queue_name" {
  description = "Name of the GPU inference ClusterQueue."
  value       = kubernetes_manifest.gpu_cluster_queue.manifest.metadata.name
}

output "local_queue_name" {
  description = "Name of the LocalQueue in the workload namespace."
  value       = kubernetes_manifest.gpu_local_queue.manifest.metadata.name
}
