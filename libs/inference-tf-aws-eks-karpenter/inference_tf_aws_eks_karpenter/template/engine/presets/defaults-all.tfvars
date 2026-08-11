region              = "us-west-2"
cluster_name_prefix = "inference"
kubernetes_version  = "1.36"
karpenter_version   = "1.14.0"
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
metrics_server_chart_version     = "3.13.1"
cluster_autoscaler_chart_version = "9.59.0"

# --- GPU serving path: always on (GPUs are mandatory for inference) ---
nvidia_device_plugin_version       = "v0.19.3"
nvidia_device_plugin_chart_version = "0.19.3"

# --- High-end GPU pool: p4d/p5/p5en, gated. The pool CR is free until a pod
# opts in (nvidia-p label + taint), so default-on is cost-safe. Set an ODCR id to pin
# it to a Capacity Reservation (P on-demand capacity is scarce).
enable_gpu_p_nodepool         = true
gpu_p_capacity_reservation_id = ""

# --- Storage: EBS gp3 default + S3-mount (Mountpoint-for-S3) ---
mountpoint_s3_csi_version = "v2.7.0-eksbuild.1"

# --- Observability: kube-prometheus-stack + DCGM + Container Insights ---
kube_prometheus_stack_chart_version = "88.1.5"
dcgm_exporter_chart_version         = "4.8.3"
nvidia_dcgm_exporter_version        = "4.6.0-4.8.3-distroless"
grafana_version                     = "13.1.2" # must match the chart's Grafana appVersion
prometheus_retention                = "15d"
prometheus_memory_limit             = "6Gi"
enable_container_insights           = true

# --- Autoscaling & orchestration operators ---
# KEDA: pod autoscaling (Prometheus/DCGM/SQS scalers). Images are ghcr.io-only, so
# all three (operator, metrics-apiserver, admission-webhooks) are VENDORED to ECR at
# this same tag (== chart appVersion). KRO: resource orchestration; chart + controller
# image both on registry.k8s.io/kro (pull-through, no vendoring). Image tag is the
# chart appVersion prefixed with "v" (registry.k8s.io/kro/kro:v<version>).
keda_chart_version = "2.20.2"
kro_chart_version  = "0.9.3"

# --- Inference-routing (opt-in, off by default) ---
# Gateway API Inference Extension (InferencePool) CRDs, for KV-aware / disaggregated
# routing. CRD-only; the Endpoint Picker + Envoy data plane ship in the workload
# chart. See platform_inference_ext.tf.
enable_inference_routing = false

# --- Image supply: common-utility images via pull-through ---
# Each entry MUST use a no-creds trusted upstream (public.ecr.aws/quay.io/registry.k8s.io).
common_images = []

# --- Cluster capacity caps (single source of truth) ---
# Each sets the Karpenter NodePool spec.limits AND the derived Kueue flavor
# nominalQuota, so admission (Kueue) can never exceed provisioning (Karpenter).
gpu_g_capacity  = 16    # g-tier GPUs (A10G/L4); also caps the g-flavor EFA quota
gpu_p_capacity  = 64    # high-tier GPUs (A100/H100/H200)
cpu_capacity    = 768   # vCPUs (CPU pool)
memory_capacity = "4Ti" # memory (CPU pool)

# --- Multi-node inference: LWS + Kueue + EFA ---
# All gated (false by default). Enable for multi-node tracks.
enable_lws        = false
lws_chart_version = "0.9.0"

enable_kueue             = false
kueue_chart_version      = "0.19.0"
kueue_cluster_queue_name = "inference-gpu"
kueue_gpu_lending_limit  = 0
workload_namespace       = "inference"

enable_efa                      = false
efa_device_plugin_chart_version = "v0.5.30"
efa_device_plugin_image_tag     = "v0.5.20" # chart v0.5.30 appVersion; vendored into ECR

# --- GPU node image-pull acceleration (containerd 2.2 parallel pull/unpack) ---
# GPU/ML images are multi-GB; AL2023 ships containerd 2.2 with parallel pull OFF.
# Enabled only on the gpu/gpu-p NodeClasses (CPU nodes pull small images).
gpu_parallel_image_pull                    = true
gpu_parallel_image_pull_max_downloads      = 20
gpu_parallel_image_pull_layer_buffer_bytes = 16777216 # 16 MiB
