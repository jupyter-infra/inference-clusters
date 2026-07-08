# All variables MUST be declared here without default values.
# Default values live in ./presets/defaults-all.tfvars.

variable "region" {
  description = "AWS region to deploy the cluster into."
  type        = string
}

variable "cluster_name_prefix" {
  description = "Prefix for the EKS cluster name; a random suffix is appended for uniqueness."
  type        = string
}

variable "kubernetes_version" {
  description = "Kubernetes control-plane version for the EKS cluster."
  type        = string
}

variable "karpenter_version" {
  description = "Version of the Karpenter Helm chart to install."
  type        = string
}

variable "custom_tags" {
  description = "Additional tags applied to all resources created by this template."
  type        = map(string)
}

# --- Platform Node Placement ---
# Operator controllers (Kueue, LWS, KRO) run on platform nodes, not GPU
# dataplane nodes. This keeps controllers off expensive GPU instances and
# ensures they survive node scaling events on the GPU pool.

variable "platform_node_selector" {
  description = "Node selector for operator controllers (pins them to platform/system nodes)."
  type        = map(string)
}

# --- Module Enable Flags ---
# All default to false so multi-node infrastructure only deploys when opted in.

variable "enable_lws" {
  description = "Install the LeaderWorkerSet CRD and controller."
  type        = bool
}

variable "enable_kro" {
  description = "Install the KRO controller."
  type        = bool
}

variable "enable_multinode_nodepool" {
  description = "Create the Karpenter NodePool/EC2NodeClass for multi-node GPU inference."
  type        = bool
}

variable "enable_nvidia_plugin" {
  description = "Install the NVIDIA device plugin and GPU Feature Discovery."
  type        = bool
}

variable "enable_efa_plugin" {
  description = "Install the AWS EFA device plugin."
  type        = bool
}

variable "enable_efs_csi" {
  description = "Install the EFS CSI driver and create model weights storage."
  type        = bool
}

# --- LeaderWorkerSet ---

variable "lws_version" {
  description = "Version of the LeaderWorkerSet Helm chart."
  type        = string
}

# --- KRO ---

variable "kro_version" {
  description = "Version of the KRO Helm chart."
  type        = string
}

# --- Kueue (admission control + gang scheduling) ---

variable "enable_kueue" {
  description = "Install Kueue for admission control and gang scheduling of LWS workloads."
  type        = bool
}

variable "kueue_version" {
  description = "Version of the Kueue Helm chart."
  type        = string
}

variable "kueue_cluster_queue_name" {
  description = "Name of the ClusterQueue for GPU inference workloads."
  type        = string
}

variable "kueue_cohort_name" {
  description = "Cohort name for capacity borrowing/lending between queues."
  type        = string
}

variable "kueue_gpu_quota" {
  description = "Nominal GPU quota for the inference ClusterQueue."
  type        = number
}

variable "kueue_gpu_lending_limit" {
  description = "Maximum GPUs lent to other queues when idle."
  type        = number
}

variable "kueue_cpu_quota" {
  description = "Nominal CPU quota for the inference ClusterQueue."
  type        = number
}

variable "kueue_memory_quota" {
  description = "Nominal memory quota for the inference ClusterQueue."
  type        = string
}

variable "kueue_workload_namespace" {
  description = "Namespace where the LocalQueue is created for inference workloads."
  type        = string
}

variable "kueue_topology_levels" {
  description = "Topology hierarchy for TAS co-location (zone → hostname ensures EFA same-AZ)."
  type        = list(string)
}

variable "kueue_wait_for_pods_ready_timeout" {
  description = "How long Kueue waits for all pods to become Ready before evicting."
  type        = string
}

variable "kueue_wait_for_pods_ready_retries" {
  description = "Number of re-queue attempts on waitForPodsReady timeout."
  type        = number
}

# --- Karpenter Multi-Node NodePool ---

variable "multinode_instance_families" {
  description = "Allowed EC2 instance families for multi-node GPU nodes."
  type        = list(string)
}

variable "multinode_capacity_types" {
  description = "Allowed capacity types for multi-node GPU nodes."
  type        = list(string)
}

variable "multinode_gpu_limit" {
  description = "Maximum total GPUs the multi-node NodePool can provision."
  type        = number
}

variable "multinode_consolidate_after" {
  description = "Duration after which empty multi-node nodes are consolidated."
  type        = string
}

variable "multinode_root_volume_size" {
  description = "Root EBS volume size for multi-node GPU nodes."
  type        = string
}

# --- NVIDIA Device Plugin ---

variable "nvidia_plugin_version" {
  description = "Version of the nvidia-device-plugin Helm chart."
  type        = string
}

variable "enable_nfd" {
  description = "Enable Node Feature Discovery alongside the GPU plugin."
  type        = bool
}

# --- EFA Device Plugin ---

variable "efa_plugin_version" {
  description = "Version of the aws-efa-k8s-device-plugin Helm chart."
  type        = string
}

# --- EFS CSI Driver ---

variable "efs_csi_version" {
  description = "Version of the aws-efs-csi-driver Helm chart."
  type        = string
}

variable "efs_csi_role_arn" {
  description = "IAM role ARN for the EFS CSI driver service account (IRSA)."
  type        = string
}

variable "efs_filesystem_id" {
  description = "EFS filesystem ID for model weight storage. Leave empty to skip PV creation."
  type        = string
}

variable "model_weights_storage_size" {
  description = "Storage size for the model weights PV."
  type        = string
}
