# === Observability ===
#
# kube-prometheus-stack (Prometheus + Grafana + Alertmanager + node-exporter +
# kube-state-metrics) + the NVIDIA DCGM exporter for GPU metrics.
# All control-loop Deployments are pinned to the tainted system NG;
# the per-node DaemonSets (node-exporter, DCGM) tolerate-all so they also
# scrape Karpenter GPU nodes.
#
# EVERY image is pinned to a pull-through URI: on the endpoints-only VPC a
# node reaches images only via ECR pull-through, and only the three no-creds
# upstreams (ecr-public/quay/registry-k8s) are proxied. Defaults that point at
# docker.io/ghcr.io are repinned or the feature disabled.

locals {
  monitoring_namespace = "monitoring"

  # Pull-through registry prefixes for the quay/registry-k8s upstreams.
  quay_registry = "${local.ecr_registry}/quay"
  k8s_registry  = "${local.ecr_registry}/registry-k8s"
}

resource "helm_release" "kube_prometheus_stack" {
  name             = "kube-prometheus-stack"
  repository       = "https://prometheus-community.github.io/helm-charts"
  chart            = "kube-prometheus-stack"
  version          = var.kube_prometheus_stack_chart_version
  namespace        = local.monitoring_namespace
  create_namespace = true

  # Values as a single YAML doc: the image-registry repins + node placement + the
  # Prometheus memory-safety limits are too nested for flat `set` entries.
  values = [yamlencode({
    # --- prometheus-operator: quay images (pull-through) + system NG placement ---
    prometheusOperator = {
      nodeSelector = local.system_node_selector
      tolerations  = [local.system_toleration]
      image = {
        registry = local.quay_registry
      }
      prometheusConfigReloader = {
        image = { registry = local.quay_registry }
      }
      # Disable the admission webhook: its cert-gen patch job pulls from ghcr.io
      # (not a no-creds pull-through upstream), and CRD-level (CEL) validation plus
      # our own review of charts/metrics covers PrometheusRule validation. Same call
      # as Karpenter's webhook.enabled=false. Native cert-gen is the P1 path
      # if hard admission validation is later required.
      admissionWebhooks = {
        enabled = false
      }
      tls = {
        enabled = false
      }
    }

    # --- Prometheus: quay image + memory-safety on the fixed system NG ---
    prometheus = {
      prometheusSpec = {
        image        = { registry = local.quay_registry }
        nodeSelector = local.system_node_selector
        tolerations  = [local.system_toleration]
        retention    = var.prometheus_retention
        # A memory LIMIT so a cardinality spike OOM-kills Prometheus in isolation
        # rather than taking down co-resident Karpenter/KEDA/CoreDNS.
        resources = {
          requests = { cpu = "250m", memory = "1Gi" }
          limits   = { memory = var.prometheus_memory_limit }
        }
      }
    }

    # --- Alertmanager: quay image + system NG ---
    alertmanager = {
      alertmanagerSpec = {
        image        = { registry = local.quay_registry }
        nodeSelector = local.system_node_selector
        tolerations  = [local.system_toleration]
      }
    }

    # --- kube-state-metrics: registry.k8s.io (pull-through) + system NG ---
    kube-state-metrics = {
      image        = { registry = local.k8s_registry }
      nodeSelector = local.system_node_selector
      tolerations  = [local.system_toleration]
    }

    # --- node-exporter: quay image; DaemonSet tolerate-all (must scrape GPU nodes) ---
    prometheus-node-exporter = {
      image = { registry = local.quay_registry }
      tolerations = [{
        operator = "Exists"
      }]
    }

    # --- Grafana: repin docker.io -> quay; sidecar already quay; system NG ---
    grafana = {
      # Opt-in (AGPL-3.0): see enable_grafana. Disabled = no Grafana Deployment.
      enabled      = var.enable_grafana
      nodeSelector = local.system_node_selector
      tolerations  = [local.system_toleration]
      image = {
        # Grafana publishes ONLY to docker.io/ghcr, which requires creds to pull-through. 
        # We vendored to our ECR via CodeBuild. The subchart builds "<registry>/<repository>",
        # so split our ECR ref: registry = the ECR host, repository = the repo path.
        registry   = local.ecr_registry
        repository = var.enable_grafana ? aws_ecr_repository.vendored["grafana"].name : ""
        tag        = local.vendored_tag
      }
      sidecar = {
        image = {
          # kiwigrid/k8s-sidecar IS on quay (verified) — reach it via pull-through
          # (registry default quay.io → our quay prefix; keep the repository path).
          registry   = local.quay_registry
          repository = "kiwigrid/k8s-sidecar"
        }
      }
      # No Terraform-managed password: access is the network posture
      # (ClusterIP + port-forward, gated by the EKS access entry). Anonymous Admin so
      # port-forward lands straight on dashboards.
      service = { type = "ClusterIP" }
      "grafana.ini" = {
        "auth.anonymous" = {
          enabled  = true
          org_role = "Admin"
        }
        auth = {
          disable_login_form = true
        }
      }
      # Let the sidecar discover dashboard ConfigMaps (charts/metrics) cluster-wide.
      sidecar_dashboards_enabled = true
    }
  })]

  depends_on = [
    null_resource.cluster_addons,
    null_resource.pullthrough_ready,
    module.node_group,
    # Grafana image is vendored to ECR (images_vendored.tf) — it must exist first.
    null_resource.image_vendor,
  ]
}

# --- NVIDIA DCGM exporter (GPU metrics) ---
#
# Image vendored from nvcr.io to gpu/dcgm-exporter via CodeBuild (images.tf); nvcr.io
# has no no-creds mirror and DCGM is NOT on Quay (verified). Its built-in
# ServiceMonitor wires Prometheus scraping (needs the CRD from the stack above).
#
# Placement: GPU nodes ONLY. DCGM initializes the NVIDIA driver on startup and
# CrashLoopBackOffs on a CPU node (no GPU/driver — verified live). So it must
# nodeSelector onto the gpu-g NodePool (inference/accelerator=nvidia-g) AND tolerate
# that pool's taint (nvidia.com/gpu=present:NoSchedule). A tolerate-ALL DaemonSet is
# WRONG here — it schedules DCGM onto CPU system/inference nodes where it can't run.
resource "helm_release" "dcgm_exporter" {
  name       = "dcgm-exporter"
  repository = "https://nvidia.github.io/dcgm-exporter/helm-charts"
  chart      = "dcgm-exporter"
  version    = var.dcgm_exporter_chart_version
  namespace  = local.monitoring_namespace

  set = [
    {
      name  = "image.repository"
      value = aws_ecr_repository.vendored["dcgm_exporter"].repository_url
    },
    { name = "image.tag", value = local.vendored_tag },
    # Run ONLY on GPU nodes (the gpu-g NodePool labels them inference/accelerator).
    { name = "nodeSelector.inference/accelerator", value = "nvidia-g" },
    # Tolerate the GPU pool taint so it CAN land there.
    { name = "tolerations[0].key", value = "nvidia.com/gpu" },
    { name = "tolerations[0].operator", value = "Exists" },
    { name = "tolerations[0].effect", value = "NoSchedule" },
    # Emit a ServiceMonitor so Prometheus scrapes GPU metrics. It MUST carry the
    # release label the stack's Prometheus selects on (serviceMonitorSelector =
    # release: kube-prometheus-stack), or Prometheus ignores it and no GPU metrics
    # flow (verified live — DCGM target absent until this label was added).
    { name = "serviceMonitor.enabled", value = "true" },
    { name = "serviceMonitor.additionalLabels.release", value = "kube-prometheus-stack" },
  ]

  depends_on = [
    helm_release.kube_prometheus_stack,
    null_resource.image_vendor,
  ]
}

# The amazon-cloudwatch-observability EKS addon (cluster-wide CW metrics + Fluent Bit
# logs on EVERY node, incl. the platform/operator managed nodes group) lives
# with the other EKS managed addons in eks_addons.tf, gated by var.enable_container_insights.

# --- First-party metrics glue (charts/metrics) ---
#
# ServiceMonitors (Karpenter, node) + Grafana dashboards/datasource ConfigMaps.
# DCGM ships its own ServiceMonitor (above); this chart adds the rest + dashboards.
resource "helm_release" "metrics" {
  name      = "metrics"
  chart     = "${path.module}/../charts/metrics"
  namespace = local.monitoring_namespace

  set = [
    { name = "karpenterNamespace", value = local.karpenter_namespace },
    # Chart content hash so editing a chart file triggers a re-apply (see main.tf).
    { name = "chartContentHash", value = local.chart_hashes["metrics"] },
  ]

  depends_on = [
    helm_release.kube_prometheus_stack,
  ]
}
