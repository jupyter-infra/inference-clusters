# All variables MUST be declared here without default values.
# Default values live in ./presets/defaults-all.tfvars.
#
# Each description follows the jd display convention: a single-line summary, then
# (optionally) a blank line, further explanation, and a Recommended/Example value.

variable "region" {
  description = <<-EOT
    The AWS region to deploy the cluster into.

    Refer to: https://docs.aws.amazon.com/global-infrastructure/latest/regions/aws-regions.html

    Example: us-west-2
  EOT
  type        = string
}

variable "cluster_name_prefix" {
  description = <<-EOT
    The prefix for the EKS cluster name.

    A random deployment suffix is appended so multiple deployments can coexist in
    the same AWS account and region.

    Recommended: inference
  EOT
  type        = string
}

variable "kubernetes_version" {
  description = <<-EOT
    The Kubernetes control-plane version for the EKS cluster.

    Refer to: https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions.html

    Recommended: 1.36
  EOT
  type        = string
}

variable "karpenter_version" {
  description = <<-EOT
    The version of the Karpenter Helm chart to install.

    Recommended: 1.14.0
  EOT
  type        = string
}

variable "custom_tags" {
  description = <<-EOT
    Additional tags applied to all resources created by this template.

    Recommended: {}
  EOT
  type        = map(string)
}

variable "cluster_log_retention_days" {
  description = <<-EOT
    Retention in days for the EKS control-plane CloudWatch log group.

    Recommended: 90
  EOT
  type        = number
}

variable "enable_nat_gateway" {
  description = <<-EOT
    The cluster egress posture.

    false (default) = endpoints-only: no NAT, no public subnets, nodes reach AWS
    only over VPC endpoints (enforces the artifacts-from-our-registry invariant).
    true = internet-egress enabled: adds an IGW + per-AZ NAT + public subnets
    for arbitrary public egress.

    Recommended: false
  EOT
  type        = bool
}

variable "bootstrap_instance_types" {
  description = <<-EOT
    The instance types for the system managed node group (control-loop pods only).

    The system NG is the sizing lever; Prometheus peak memory is the binding
    constraint, so the default is a 16 GB SKU rather than 8 GB.

    Recommended: ["m6i.xlarge"]
  EOT
  type        = list(string)
}

variable "bootstrap_desired_size" {
  description = <<-EOT
    The desired size of the system managed node group.

    Cluster Autoscaler moves this within min/max.

    Recommended: 2
  EOT
  type        = number
}

variable "bootstrap_min_size" {
  description = <<-EOT
    The minimum size of the system managed node group.

    Recommended: 2
  EOT
  type        = number
}

variable "bootstrap_max_size" {
  description = <<-EOT
    The maximum size of the system managed node group.

    Recommended: 6
  EOT
  type        = number
}

variable "admin_role_names" {
  description = <<-EOT
    IAM role names granted EKS cluster-admin via access entries.

    Granted in addition to the deploying caller.

    Recommended: []
  EOT
  type        = list(string)
}

variable "admin_user_names" {
  description = <<-EOT
    IAM user names granted EKS cluster-admin via access entries.

    Granted in addition to the deploying caller.

    Recommended: []
  EOT
  type        = list(string)
}

variable "metrics_server_chart_version" {
  description = <<-EOT
    The Helm chart version for metrics-server (HPA + kubectl top).

    Recommended: 3.13.1
  EOT
  type        = string
}

variable "cluster_autoscaler_chart_version" {
  description = <<-EOT
    The Helm chart version for the Kubernetes Cluster Autoscaler (system MNG scaling).

    The image tag is derived from kubernetes_version (v<minor>.0), not this chart version.

    Recommended: 9.59.0
  EOT
  type        = string
}

variable "nvidia_device_plugin_version" {
  description = <<-EOT
    The nvcr.io/nvidia/k8s-device-plugin image tag to vendor into ECR.

    Recommended: v0.19.3
  EOT
  type        = string
}

variable "nvidia_device_plugin_chart_version" {
  description = <<-EOT
    The Helm chart version for the NVIDIA device plugin.

    Recommended: 0.19.3
  EOT
  type        = string
}

variable "mountpoint_s3_csi_version" {
  description = <<-EOT
    The EKS addon version for the Mountpoint-for-S3 CSI driver (S3-mount StorageClass).

    Recommended: v2.7.0-eksbuild.1
  EOT
  type        = string
}

variable "nvidia_dcgm_exporter_version" {
  description = <<-EOT
    The nvcr.io/nvidia/k8s/dcgm-exporter image tag to vendor into ECR (GPU metrics).

    Recommended: 4.6.0-4.8.3-distroless
  EOT
  type        = string
}

variable "kube_prometheus_stack_chart_version" {
  description = <<-EOT
    The Helm chart version for kube-prometheus-stack (Prometheus + Grafana + Alertmanager).

    Recommended: 88.1.5
  EOT
  type        = string
}

variable "dcgm_exporter_chart_version" {
  description = <<-EOT
    The Helm chart version for the NVIDIA DCGM exporter.

    Recommended: 4.8.3
  EOT
  type        = string
}

variable "grafana_version" {
  description = <<-EOT
    The docker.io/grafana/grafana image tag to vendor into ECR.

    Grafana has no no-creds registry, so it is vendored. This MUST match the
    kube-prometheus-stack chart's Grafana appVersion.

    Recommended: 13.1.2
  EOT
  type        = string
}

variable "prometheus_retention" {
  description = <<-EOT
    The Prometheus metrics retention window.

    Recommended: 15d
  EOT
  type        = string
}

variable "prometheus_memory_limit" {
  description = <<-EOT
    The memory limit on the Prometheus pod.

    A cardinality spike OOM-kills it in isolation rather than taking down
    co-resident control-loop pods.

    Recommended: 6Gi
  EOT
  type        = string
}

variable "enable_container_insights" {
  description = <<-EOT
    Whether to install the CloudWatch Observability addon (Container Insights + Fluent Bit pod logs).

    Recommended: true
  EOT
  type        = bool
}

variable "keda_chart_version" {
  description = <<-EOT
    The Helm chart version for KEDA (pod autoscaling).

    The chart appVersion equals this, and it is also the image tag vendored into
    ECR (KEDA images are published only to ghcr.io, so all three are vendored).

    Recommended: 2.20.2
  EOT
  type        = string
}

variable "kro_chart_version" {
  description = <<-EOT
    The Helm chart and controller image version for KRO (resource orchestration).

    Both the chart and image come from registry.k8s.io/kro and are reached via ECR
    pull-through.

    Recommended: 0.9.3
  EOT
  type        = string
}

variable "enable_gpu_p_nodepool" {
  description = <<-EOT
    Whether to install the high-end GPU NodePool (p4d/p5/p5en — A100/H100/H200).

    A P node is expensive and quota-constrained; the pool CR costs nothing until a
    pod opts into it via the nvidia-p label + taint toleration.

    Recommended: true
  EOT
  type        = bool
}

variable "gpu_p_capacity_reservation_id" {
  description = <<-EOT
    An optional On-Demand Capacity Reservation (ODCR) id to pin the gpu-p pool to.

    P on-demand capacity is scarce, so orgs often reserve it. Empty = plain
    on-demand.

    Recommended: ""
  EOT
  type        = string
}

variable "enable_inference_routing" {
  description = <<-EOT
    Whether to install the Gateway API Inference Extension CRDs (InferencePool).

    Needed by tracks that do KV-aware / disaggregated routing (an Endpoint Picker
    watches an InferencePool). Off by default; the CRDs cost nothing until a workload
    declares an InferencePool.

    Recommended: false
  EOT
  type        = bool
}


variable "common_images" {
  description = <<-EOT
    Common-utility image paths (busybox/certgen-class) made available to all nodes via ECR pull-through.

    Each entry is a full image path INCLUDING the registry host, and MUST resolve
    to a no-credentials trusted upstream (public.ecr.aws, quay.io, registry.k8s.io)
    — Docker Hub is not trusted, so use the ECR Public mirror (e.g.
    public.ecr.aws/docker/library/busybox). The deployer adds paths, not registries.

    Recommended: []
  EOT
  type        = list(string)

  validation {
    condition = alltrue([
      for img in var.common_images :
      can(regex("^(public\\.ecr\\.aws|quay\\.io|registry\\.k8s\\.io)/", img))
    ])
    error_message = "Every common_images entry must start with a trusted no-credentials registry host: public.ecr.aws/, quay.io/, or registry.k8s.io/ (Docker Hub/GHCR require credentials and are not supported; use the ECR Public mirror instead)."
  }
}

# === Multi-node inference (LWS + Kueue + EFA) ===

variable "enable_lws" {
  description = <<-EOT
    Install the LeaderWorkerSet controller for multi-node pod group lifecycle.

    Required for multi-node inference — manages leader/worker templates with
    RecreateGroupOnPodRestart semantics (NCCL groups are not recoverable).

    Recommended: true (for multi-node tracks)
  EOT
  type        = bool
}

variable "lws_chart_version" {
  description = <<-EOT
    The Helm chart version for LeaderWorkerSet (oci://registry.k8s.io/lws/charts/lws).

    Published to registry.k8s.io (pull-through, no vendoring).

    Recommended: 0.9.0
  EOT
  type        = string
}

variable "enable_kueue" {
  description = <<-EOT
    Install Kueue for admission control and gang scheduling of LWS workloads.

    Kueue gates workloads behind GPU quota — the entire LWS group is admitted
    atomically or stays suspended. Includes a Prometheus ServiceMonitor and
    waitForPodsReady (evicts + requeues on partial provisioning). AZ co-location for
    multi-node NCCL/EFA is enforced by the LWS exclusive-topology annotation, not Kueue TAS.

    Requires enable_lws = true (LWS CRD must exist for Kueue's integration).

    Recommended: true (for multi-node tracks)
  EOT
  type        = bool
}

variable "kueue_chart_version" {
  description = <<-EOT
    The Helm chart version for Kueue (oci://registry.k8s.io/kueue/charts/kueue).

    Published to registry.k8s.io (pull-through, no vendoring).

    Recommended: 0.19.0
  EOT
  type        = string
}

variable "workload_namespace" {
  description = <<-EOT
    The shared Kubernetes namespace where inference workloads run.

    Created unconditionally by the engine and referenced by platform components (the Kueue
    LocalQueue, and any future shared RBAC/quota). Owned here so it outlives optional
    operators — toggling one off never deletes the namespace or the workloads in it.

    Recommended: inference
  EOT
  type        = string
}

variable "kueue_cluster_queue_name" {
  description = <<-EOT
    Name of the ClusterQueue for GPU inference workloads.

    Recommended: inference-gpu
  EOT
  type        = string
}

variable "gpu_g_capacity" {
  description = <<-EOT
    Max g-tier GPUs (A10G/L4) the cluster may provision — the g NodePool's cap.

    This is the SINGLE source of truth for g-tier GPU capacity: it sets the
    Karpenter gpu-g NodePool spec.limits AND the Kueue gpu-g flavor nominalQuota
    (for both nvidia.com/gpu and vpc.amazonaws.com/efa, since EFA rides on GPU
    nodes), so Kueue never admits more than Karpenter will provision.

    Recommended: 16
  EOT
  type        = number
}

variable "gpu_p_capacity" {
  description = <<-EOT
    Max high-tier GPUs (A100/H100/H200) the cluster may provision — the P NodePool's cap.

    Single source of truth for P-tier GPU capacity: sets the Karpenter gpu-p
    NodePool spec.limits AND the Kueue gpu-multinode flavor nominalQuota.

    Recommended: 64
  EOT
  type        = number
}

variable "kueue_gpu_lending_limit" {
  description = <<-EOT
    Maximum GPUs lent to other queues in the cohort when idle.

    0 = no lending (all GPUs reserved for inference).

    Recommended: 0
  EOT
  type        = number
}

variable "cpu_capacity" {
  description = <<-EOT
    Max vCPUs the CPU NodePool may provision — the CPU pool's cap.

    Single source of truth for CPU capacity: sets the Karpenter cpu NodePool
    spec.limits AND the Kueue cpu-default flavor nominalQuota.

    Recommended: 768
  EOT
  type        = number
}

variable "memory_capacity" {
  description = <<-EOT
    Max memory the CPU NodePool may provision (e.g. 4Ti) — the CPU pool's cap.

    Single source of truth for memory capacity: sets the Karpenter cpu NodePool
    spec.limits AND the Kueue cpu-default flavor nominalQuota.

    Recommended: 4Ti
  EOT
  type        = string
}


variable "enable_efa" {
  description = <<-EOT
    Install the AWS EFA device plugin for multi-node NCCL networking.

    Required for multi-node inference with cross-node TP. Advertises EFA
    interfaces as allocatable resources on GPU nodes.

    Recommended: true (for multi-node tracks on p4d/p5/p5en)
  EOT
  type        = bool
}

variable "efa_device_plugin_chart_version" {
  description = <<-EOT
    The Helm chart version for the AWS EFA device plugin (eks-charts repo).

    The image is vendored into our ECR from the EKS-managed regional registry
    (inferred at apply from the vpc-cni add-on, never hardcoded) and the release
    is repinned to it. Chart version diverges from the image appVersion — set
    the image tag via efa_device_plugin_image_tag.

    Recommended: v0.5.30
  EOT
  type        = string
}

variable "efa_device_plugin_image_tag" {
  description = <<-EOT
    Image tag of the EFA device plugin to vendor (the chart's appVersion).

    The chart version and the image appVersion diverge; this is the image tag
    (not the chart version). It is vendored from the inferred EKS regional ECR
    into our own ECR, and the release's image.tag is pinned to it.

    Recommended: v0.5.20 (appVersion of chart v0.5.30)
  EOT
  type        = string
}

# === FSx for Lustre (opt-in) ===

variable "enable_fsx" {
  description = <<-EOT
    Install the FSx for Lustre RWX shared file system (weight cache + shared scratch).

    Opt-in: adds a PERSISTENT_2 SSD file system in the first private subnet, a Data
    Repository Association to the model store bucket's models/ prefix, the aws-fsx-csi-driver
    Helm release, and a static PV/PVC exposing /models over Lustre. Off by default because
    an FSx file system has a non-trivial hourly cost floor and is single-AZ.

    Recommended: false
  EOT
  type        = bool
}

variable "fsx_storage_capacity_gib" {
  description = <<-EOT
    FSx for Lustre storage capacity in GiB.

    PERSISTENT_2 SSD requires multiples of 1200. Storage size and per-unit throughput
    together set aggregate throughput (capacity_gib × per_unit_throughput / 1024).

    Recommended: 4800
  EOT
  type        = number
}

variable "fsx_per_unit_storage_throughput" {
  description = <<-EOT
    FSx for Lustre per-unit throughput in MB/s per TiB (PERSISTENT_2 SSD).

    Allowed values: 125, 250, 500, 1000. Bump for P4d/P5-heavy fleets or heavy
    training checkpoint traffic.

    Recommended: 250
  EOT
  type        = number
}

variable "fsx_kms_key_arn" {
  description = <<-EOT
    Customer-managed KMS key ARN for FSx encryption at rest.

    FSx enforces encryption at rest always; this variable only selects which key.
    Empty string (the default) means the AWS-managed aws/fsx key — zero cost, no
    customer control over rotation or cross-account grants. Setting a KMS ARN
    switches to that customer-managed key (rotation control, key-policy audit,
    cross-service auth via `kms:ViaService = fsx.<region>.amazonaws.com`).
    The template does not auto-create a CMK; if you want one, pre-create it and
    pass its ARN here.

    Recommended: ""
  EOT
  type        = string
}

variable "fsx_csi_driver_chart_version" {
  description = <<-EOT
    Helm chart version for the aws-fsx-csi-driver.

    Sourced from https://kubernetes-sigs.github.io/aws-fsx-csi-driver.

    Recommended: 1.17.0
  EOT
  type        = string
}
