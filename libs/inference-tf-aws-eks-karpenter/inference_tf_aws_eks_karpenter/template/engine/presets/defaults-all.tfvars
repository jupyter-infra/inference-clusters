region              = "us-west-2"
cluster_name_prefix = "inference"
kubernetes_version  = "1.36"
karpenter_version   = "1.13.0"
custom_tags         = {}

# --- Control-plane endpoint: open knock-surface, IAM access entries gate ---
cluster_log_retention_days = 90

# --- Egress posture: endpoints-only by default ---
enable_nat_gateway = false

# --- System managed node group: control-loop pods only ---
# m6i.xlarge = 4 vCPU / 16 GB; Prometheus peak memory is the binding constraint.
bootstrap_instance_types = ["m6i.xlarge"]
bootstrap_desired_size   = 2
bootstrap_min_size       = 2
bootstrap_max_size       = 6

# --- Cluster access ---
admin_role_names = []
admin_user_names = []

# --- Karpenter / platform charts ---
metrics_server_chart_version     = "3.12.2"
cluster_autoscaler_chart_version = "9.58.0"

# --- GPU serving path: always on (GPUs are mandatory for inference) ---
nvidia_device_plugin_version       = "v0.17.1"
nvidia_device_plugin_chart_version = "0.17.1"

# --- High-end GPU pool: p4d/p5/p5en, gated. The pool CR is free until a pod
# opts in (nvidia-p label + taint), so default-on is cost-safe. Set an ODCR id to pin
# it to a Capacity Reservation (P on-demand capacity is scarce).
enable_gpu_p_nodepool         = true
gpu_p_capacity_reservation_id = ""

# --- Storage: EBS gp3 default + S3-mount (Mountpoint-for-S3) ---
mountpoint_s3_csi_version = "v2.7.0-eksbuild.1"

# --- Observability: kube-prometheus-stack + DCGM + Container Insights ---
kube_prometheus_stack_chart_version = "87.6.0"
dcgm_exporter_chart_version         = "4.8.2"
nvidia_dcgm_exporter_version        = "4.5.3-4.8.2-distroless"
grafana_version                     = "13.1.0" # must match the chart's Grafana appVersion
prometheus_retention                = "15d"
prometheus_memory_limit             = "6Gi"
enable_container_insights           = true

# --- Autoscaling & orchestration operators ---
# KEDA: pod autoscaling (Prometheus/DCGM/SQS scalers). Images are ghcr.io-only, so
# all three (operator, metrics-apiserver, admission-webhooks) are VENDORED to ECR at
# this same tag (== chart appVersion). KRO: resource orchestration; chart + controller
# image both on registry.k8s.io/kro (pull-through, no vendoring). Image tag is the
# chart appVersion prefixed with "v" (registry.k8s.io/kro/kro:v<version>).
keda_chart_version = "2.20.1"
kro_chart_version  = "0.9.2"

# --- Image supply: common-utility images via pull-through ---
# Each entry MUST use a no-creds trusted upstream (public.ecr.aws/quay.io/registry.k8s.io).
common_images = []

# --- Multi-node inference: LWS + Kueue + EFA ---
# All gated (false by default). Enable for multi-node tracks.
enable_lws        = false
lws_chart_version = "0.9.0"

enable_kueue             = false
kueue_chart_version      = "0.18.2"
workload_namespace       = "inference"
kueue_cluster_queue_name = "inference-gpu"
kueue_gpu_quota          = 64
kueue_gpu_lending_limit  = 0
kueue_cpu_quota          = 768
kueue_memory_quota       = "4Ti"

enable_efa                      = false
efa_device_plugin_chart_version = "v0.5.29"
