variable "kueue_namespace" {
  description = "Namespace to install the Kueue controller."
  type        = string
}

variable "kueue_version" {
  description = "Version of the Kueue Helm chart."
  type        = string
}

variable "cluster_queue_name" {
  description = "Name of the ClusterQueue for GPU inference workloads."
  type        = string
}

variable "cohort_name" {
  description = "Cohort name for capacity borrowing/lending between queues."
  type        = string
}

variable "gpu_quota" {
  description = "Nominal GPU quota for the inference ClusterQueue."
  type        = number
}

variable "gpu_lending_limit" {
  description = "Maximum GPUs lent to other queues in the cohort when idle."
  type        = number
}

variable "cpu_quota" {
  description = "Nominal CPU quota for the inference ClusterQueue."
  type        = number
}

variable "memory_quota" {
  description = "Nominal memory quota for the inference ClusterQueue (e.g. '2Ti')."
  type        = string
}

variable "workload_namespace" {
  description = "Namespace where the LocalQueue is created for inference workloads."
  type        = string
}

variable "topology_levels" {
  description = "Topology hierarchy for TAS co-location (zone → hostname ensures EFA same-AZ)."
  type        = list(string)
}

variable "wait_for_pods_ready_timeout" {
  description = "How long Kueue waits for all pods to become Ready before evicting the workload."
  type        = string
}

variable "wait_for_pods_ready_retries" {
  description = "Number of times Kueue re-queues a workload that fails waitForPodsReady."
  type        = number
}
