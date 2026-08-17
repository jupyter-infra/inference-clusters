output "deployment_id" {
  description = "Unique identifier for this deployment, suffixed onto resource names."
  value       = random_id.postfix.hex
}

output "region" {
  description = "AWS region the cluster is deployed into."
  value       = data.aws_region.current.id
}

output "cluster_name" {
  description = "Name of the EKS cluster."
  value       = module.eks_cluster.cluster_name
}

output "cluster_endpoint" {
  description = "API server endpoint URL for the EKS cluster."
  value       = module.eks_cluster.cluster_endpoint
}

output "platform_mng_names" {
  description = "Names of the EKS managed node groups."
  # tolist() coerces the bracket literal from tuple(string) to list(string); a bare
  # [ ... ] literal is typed as a tuple, which the jd output parser doesn't recognize.
  # `pool.status` reads this to branch its is-mng flag (MNG vs Karpenter NodePool).
  value = tolist([module.node_group.node_group_name])
}

output "cluster_arn" {
  description = "ARN of the EKS cluster."
  value       = module.eks_cluster.cluster_arn
}

output "cluster_ca_certificate" {
  description = "Base64-encoded CA certificate for the EKS cluster."
  value       = module.eks_cluster.cluster_ca_certificate
  sensitive   = true
}

output "vpc_id" {
  description = "ID of the VPC hosting the EKS cluster."
  value       = module.vpc.vpc_id
}

output "kubeconfig_path" {
  description = "Path to the local kubeconfig file for this cluster."
  value       = abspath("${path.root}/.kube/config")
}

output "model_store_bucket" {
  description = "Name of the shared S3 model store bucket (weights under models/)."
  value       = module.model_store.bucket_name
}

output "model_store_bucket_arn" {
  description = "ARN of the shared S3 model store bucket."
  value       = module.model_store.bucket_arn
}

output "batch_intake_bucket" {
  description = "Name of the S3 bucket batch-inference requests flow into."
  value       = module.batch_intake.bucket_name
}

output "batch_intake_bucket_arn" {
  description = "ARN of the S3 batch-inference intake bucket."
  value       = module.batch_intake.bucket_arn
}

output "batch_output_bucket" {
  description = "Name of the S3 bucket batch-inference results and metrics land in."
  value       = module.batch_output.bucket_name
}

output "batch_output_bucket_arn" {
  description = "ARN of the S3 batch-inference output bucket."
  value       = module.batch_output.bucket_arn
}

output "batch_inference_service_account_name" {
  description = "Name of the service account that has batch S3 access through Pod Identity."
  value       = kubernetes_service_account_v1.batch_inference.metadata[0].name
}

output "batch_storage_config_map_name" {
  description = "Name of the ConfigMap that contains the batch bucket names and AWS Region."
  value       = kubernetes_config_map_v1.batch_storage.metadata[0].name
}

output "workload_namespace" {
  description = "Namespace for inference workloads and shared batch resources."
  value       = kubernetes_namespace_v1.workload.metadata[0].name
}

output "keda_namespace" {
  description = "Namespace the KEDA autoscaling operator runs in."
  value       = local.keda_namespace
}

output "kro_namespace" {
  description = "Namespace the KRO orchestration operator runs in."
  value       = local.kro_namespace
}

# --- jd health namespaces + vendored images ---
# Namespaces the platform operators run in — the health `components:` block resolves each
# component's `scope` to one of these to status-check the right Deployment.

output "karpenter_namespace" {
  description = "Namespace the Karpenter controller (and other kube-system platform add-ons) runs in."
  value       = local.karpenter_namespace
}

output "kube_system_namespace" {
  description = "The kube-system namespace (scope for the NVIDIA device-plugin DaemonSet/chart health components)."
  value       = "kube-system"
}

output "monitoring_namespace" {
  description = "Namespace the monitoring stack (kube-prometheus-stack, Grafana) runs in."
  value       = local.monitoring_namespace
}

# Vendored-image ECR repo names + shared tag — the health `images:` block resolves each
# image's repository-output/tag-output from these to confirm the air-gapped vendoring
# landed in ECR. Per-image string outputs (the jd images layer resolves one string each;
# a map output would crash the jd output parser).

output "vendored_image_tag" {
  description = "The tag every platform image is vendored to ECR under (image-vendor CodeBuild job)."
  value       = local.vendored_tag
}

output "keda_operator_ecr_repository" {
  description = "ECR repository name of the vendored KEDA operator image."
  value       = aws_ecr_repository.vendored["keda_operator"].name
}

output "keda_metrics_apiserver_ecr_repository" {
  description = "ECR repository name of the vendored KEDA metrics-apiserver image."
  value       = aws_ecr_repository.vendored["keda_metrics_apiserver"].name
}

output "keda_admission_webhooks_ecr_repository" {
  description = "ECR repository name of the vendored KEDA admission-webhooks image."
  value       = aws_ecr_repository.vendored["keda_admission_webhooks"].name
}

output "grafana_ecr_repository" {
  description = "ECR repository name of the vendored Grafana image."
  value       = aws_ecr_repository.vendored["grafana"].name
}

output "dcgm_exporter_ecr_repository" {
  description = "ECR repository name of the vendored DCGM exporter image."
  value       = aws_ecr_repository.vendored["dcgm_exporter"].name
}

output "device_plugin_ecr_repository" {
  description = "ECR repository name of the vendored NVIDIA device-plugin image."
  value       = aws_ecr_repository.vendored["device_plugin"].name
}

output "starter_rgd_names" {
  description = "Names of the starter KRO ResourceGroups that consumer workloads can instantiate or fork."
  # tolist() forces list(string); a bare ["..."] literal types as tuple, which the
  # jd output parser does not support (NotImplementedError on ['tuple', ['string']]).
  value = tolist(["inference-deployment"])
}

# --- consumer-facing outputs ---

output "ecr_registry" {
  description = "Base URI of the private ECR registry consumer workload images are vendored into."
  value       = local.ecr_registry
}

output "workload_repo_prefix" {
  description = "Cluster-scoped ECR prefix the onboarder vendors consumer workload images under (e.g. <cluster>/workload)."
  value       = local.workload_repo_prefix
}

output "onboarder_codebuild_project" {
  description = "Name of the onboarder CodeBuild project to imports model weights and component images."
  value       = module.onboarder.project_name
}

output "image_build_codebuild_project" {
  description = "Name of the image-build CodeBuild project that builds a source dir (uploaded to image-build/in) into a workload/* ECR image. For images with no published upstream to import."
  value       = module.image_build.project_name
}

output "image_build_input_s3_uri" {
  description = "S3 URI prefix a consumer uploads its build source tarball (Dockerfile + context) to before triggering the image-build job."
  value       = local.image_build_in_s3_uri
}

output "models_s3_uri" {
  description = "S3 URI prefix where the onboard job copies the model weights."
  value       = local.models_s3_uri
}

output "onboarder_input_s3_uri" {
  description = "S3 URI prefix where the onboard job outputs a consumer chart tarball."
  value       = local.rehost_in_s3_uri
}

output "onboarder_output_s3_uri" {
  description = "S3 URI prefix where the onboard job writes the overrides.yaml."
  value       = local.rehost_out_s3_uri
}

output "trusted_upstream_registries" {
  description = "No-credentials registry hosts a chart image may reference (resolved via ECR pull-through).."
  value       = sort([for u in local.trusted_upstreams : u.url])
}

# --- FSx for Lustre outputs (empty strings / null when var.enable_fsx is false) ---

output "fsx_enabled" {
  description = "Whether the FSx for Lustre shared file system is provisioned (\"true\"/\"false\")."
  # tostring() so the value is a string, not a bool — jupyter-deploy's TF output-def
  # parser (tf_outdefs.py) only classifies string / list[str] / number and raises
  # NotImplementedError on `type: bool`, which fails `jd up`'s push-to-store step.
  value = tostring(var.enable_fsx)
}

output "fsx_file_system_id" {
  description = "ID of the shared FSx for Lustre file system (empty when disabled)."
  value       = var.enable_fsx ? aws_fsx_lustre_file_system.shared[0].id : ""
}

output "fsx_file_system_arn" {
  description = "ARN of the shared FSx for Lustre file system (empty when disabled)."
  value       = var.enable_fsx ? aws_fsx_lustre_file_system.shared[0].arn : ""
}

output "fsx_dns_name" {
  description = "DNS name of the shared FSx for Lustre file system (empty when disabled)."
  value       = var.enable_fsx ? aws_fsx_lustre_file_system.shared[0].dns_name : ""
}

output "fsx_mount_name" {
  description = "Lustre mount name for the shared file system — second half of the CSI volumeHandle (empty when disabled)."
  value       = var.enable_fsx ? aws_fsx_lustre_file_system.shared[0].mount_name : ""
}

output "fsx_availability_zone" {
  description = "AZ the FSx file system lives in — pin FSx-consumer pods here via topology.kubernetes.io/zone (empty when disabled)."
  value       = var.enable_fsx ? data.aws_subnet.fsx[0].availability_zone : ""
}

output "fsx_data_repository_path" {
  description = "S3 URI the /models mount is linked to via the DRA (empty when disabled)."
  value       = var.enable_fsx ? aws_fsx_data_repository_association.models[0].data_repository_path : ""
}
