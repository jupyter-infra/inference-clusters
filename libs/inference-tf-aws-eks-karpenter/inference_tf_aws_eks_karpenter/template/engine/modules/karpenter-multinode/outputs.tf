output "node_pool_name" {
  description = "Name of the Karpenter NodePool for multi-node inference."
  value       = kubernetes_manifest.gpu_node_pool.manifest.metadata.name
}

output "node_class_name" {
  description = "Name of the EC2NodeClass for multi-node inference."
  value       = kubernetes_manifest.gpu_node_class.manifest.metadata.name
}
