region              = "us-west-2"
cluster_name_prefix = "inference"
kubernetes_version  = "1.33"
karpenter_version   = "1.6.0"
custom_tags         = {}

# Module enable flags — all off by default.
# Set to true to opt into multi-node inference infrastructure.
enable_lws                = false
enable_kro                = false
enable_kueue              = false
enable_multinode_nodepool = false
enable_nvidia_plugin      = false
enable_efa_plugin         = false
enable_efs_csi            = false

# LeaderWorkerSet
lws_version = "0.6.2"

# KRO
kro_version = "0.2.1"

# Kueue (admission control + gang scheduling for LWS workloads)
kueue_version            = "0.10.0"
kueue_cluster_queue_name = "inference-gpu"
kueue_cohort_name        = "gpu-cohort"
kueue_gpu_quota          = 64
kueue_gpu_lending_limit  = 0
kueue_cpu_quota          = 768
kueue_memory_quota       = "4Ti"
kueue_workload_namespace          = "inference"
kueue_topology_levels             = ["topology.kubernetes.io/zone", "kubernetes.io/hostname"]
kueue_wait_for_pods_ready_timeout = "15m"
kueue_wait_for_pods_ready_retries = 3

# Karpenter Multi-Node NodePool
multinode_instance_families  = ["p5", "p4d"]
multinode_capacity_types     = ["on-demand"]
multinode_gpu_limit          = 64
multinode_consolidate_after  = "60s"
multinode_root_volume_size   = "500Gi"

# NVIDIA Device Plugin
nvidia_plugin_version = "0.17.0"
enable_nfd            = true

# EFA Device Plugin
efa_plugin_version = "0.5.7"

# EFS CSI Driver
efs_csi_version          = "3.1.1"
efs_csi_role_arn         = ""
efs_filesystem_id        = ""
model_weights_storage_size = "2Ti"
