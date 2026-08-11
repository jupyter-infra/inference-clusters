# === Karpenter ===
#
# "platform_" file = control-loop components pinned to the tainted system MNG.
# This file owns: the interruption SQS queue + EventBridge rules, the
# Karpenter controller helm release + Pod Identity association, the NVIDIA device
# plugin, metrics-server, and the charts/karpenter NodePool/EC2NodeClass release.

locals {
  karpenter_namespace = "kube-system"

  # Every controller chart on the tainted system NG carries this toleration +
  # nodeSelector. Karpenter-launched inference nodes carry neither.
  system_node_selector = { "inference/role" = "system" }
}

# --- Interruption queue + EventBridge ---
#
# Karpenter watches this SQS queue for AWS-initiated interruptions and drains the
# node gracefully before reclamation. No spot-interruption rule — all pools are
# on-demand; the queue is retained for maintenance/health/rebalance events.

resource "aws_sqs_queue" "karpenter_interruption" {
  name                      = local.karpenter_queue_name
  message_retention_seconds = 300
  sqs_managed_sse_enabled   = true
  tags                      = local.combined_tags
}

data "aws_iam_policy_document" "karpenter_interruption_queue" {
  statement {
    sid       = "AllowEventBridgeToSendMessages"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.karpenter_interruption.arn]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com", "sqs.amazonaws.com"]
    }
  }
}

resource "aws_sqs_queue_policy" "karpenter_interruption" {
  queue_url = aws_sqs_queue.karpenter_interruption.url
  policy    = data.aws_iam_policy_document.karpenter_interruption_queue.json
}

locals {
  # EventBridge source patterns Karpenter consumes (no spot rule).
  karpenter_event_rules = {
    scheduled_change = {
      description = "AWS health events (scheduled maintenance)"
      pattern     = { source = ["aws.health"], "detail-type" = ["AWS Health Event"] }
    }
    instance_state_change = {
      description = "EC2 instance state-change notifications"
      pattern     = { source = ["aws.ec2"], "detail-type" = ["EC2 Instance State-change Notification"] }
    }
    rebalance = {
      description = "EC2 instance rebalance recommendations"
      pattern     = { source = ["aws.ec2"], "detail-type" = ["EC2 Instance Rebalance Recommendation"] }
    }
  }
}

resource "aws_cloudwatch_event_rule" "karpenter" {
  for_each      = local.karpenter_event_rules
  name          = "${local.resource_name_prefix}-karpenter-${each.key}"
  description   = each.value.description
  event_pattern = jsonencode(each.value.pattern)
  tags          = local.combined_tags
}

resource "aws_cloudwatch_event_target" "karpenter" {
  for_each  = local.karpenter_event_rules
  rule      = aws_cloudwatch_event_rule.karpenter[each.key].name
  target_id = "KarpenterInterruptionQueue"
  arn       = aws_sqs_queue.karpenter_interruption.arn
}

# --- Karpenter controller (helm) + Pod Identity ---

resource "aws_eks_pod_identity_association" "karpenter" {
  cluster_name    = module.eks_cluster.cluster_name
  namespace       = local.karpenter_namespace
  service_account = "karpenter"
  role_arn        = module.karpenter_controller_role.role_arn
}

resource "helm_release" "karpenter" {
  name       = "karpenter"
  repository = "oci://public.ecr.aws/karpenter"
  chart      = "karpenter"
  version    = var.karpenter_version
  namespace  = local.karpenter_namespace

  # NO chart-pull auth: public.ecr.aws serves the Karpenter CHART anonymously.
  # Explicit image pin to the pull-through URI (PRIMARY resolution).
  # Pin tag AND clear the chart's default digest: a repository@sha256 ref forces
  # that exact upstream digest, which pull-through has NOT imported (import is
  # tag-triggered and ECR re-digests on import) → NotFound. A tag-only ref lets
  # pull-through import-on-miss fire.
  set = [
    {
      # Two replicas so a leader failover (node drain/consolidation) keeps a warm
      # standby. Chart already defaults to 2 + a PDB; pinned here so the intent
      # survives a chart-default change.
      name  = "replicas"
      value = "2"
    },
    {
      name  = "controller.image.repository"
      value = "${local.ecr_registry}/ecr-public/karpenter/controller"
    },
    {
      name  = "controller.image.tag"
      value = var.karpenter_version
    },
    {
      name  = "controller.image.digest"
      value = ""
    },
    {
      name  = "settings.clusterName"
      value = module.eks_cluster.cluster_name
    },
    {
      # Explicit endpoint lets Karpenter skip a DescribeCluster call (blueprint).
      name  = "settings.clusterEndpoint"
      value = module.eks_cluster.cluster_endpoint
    },
    {
      name  = "settings.interruptionQueue"
      value = aws_sqs_queue.karpenter_interruption.name
    },
    {
      # v1 chart: disable the validating webhook (blueprint) — avoids webhook/cert
      # races on a fresh cluster; CRD validation still applies.
      name  = "webhook.enabled"
      value = "false"
    },
    {
      name  = "nodeSelector.inference/role"
      value = "system"
    },
    {
      name  = "tolerations[0].key"
      value = "inference/role"
    },
    {
      name  = "tolerations[0].operator"
      value = "Equal"
    },
    {
      name  = "tolerations[0].value"
      value = "system"
    },
    {
      name  = "tolerations[0].effect"
      value = "NoSchedule"
    },
  ]

  depends_on = [
    null_resource.cluster_addons,
    null_resource.pullthrough_ready,
    module.node_group,
    aws_eks_pod_identity_association.karpenter,
    aws_sqs_queue.karpenter_interruption,
  ]
}

# --- Destroy-time drain poller ---
#
# The orphan trap: helm uninstall of the NodePool release returns as soon as the
# CRs are deleted from the API — NOT when Karpenter's async finalizer reconcile
# has actually terminated the nodes. Without this, Terraform would immediately
# destroy the controller release next, killing the only thing that drains those
# nodes → orphaned EC2/EBS/ENIs → VPC won't delete → jd down hangs.
#
# This null_resource sits BETWEEN them:
#   - NodePool release depends_on THIS  → on destroy, NodePools deleted first,
#     then this poller runs.
#   - THIS references (as real attributes, in triggers) the controller release,
#     the cluster name/endpoint, and the admin access entry → all torn down AFTER
#     the poller. So Karpenter is alive to reconcile, the API is reachable, and
#     the poller's identity still has authz for the entire drain.
# A captured string instead of an attribute ref would silently drop the edge.
resource "null_resource" "karpenter_drain" {
  triggers = {
    cluster_name = module.eks_cluster.cluster_name
    region       = var.region

    # Attribute refs = the load-bearing destroy edges (keep these alive until the
    # poller finishes). Referenced only to force ordering; values are incidental.
    controller_release = helm_release.karpenter.id
    cluster_endpoint   = module.eks_cluster.cluster_endpoint
    admin_access_entry = join(",", [for e in aws_eks_access_entry.admin_role : e.id])
    node_access_entry  = aws_eks_access_entry.node.id

    # Inlined script survives templatefile source deletion at destroy time.
    script = templatefile("${path.module}/local-destroy-cleanup.sh.tftpl", {
      cluster_name = module.eks_cluster.cluster_name
      region       = var.region
      vpc_id       = module.vpc.vpc_id
    })
  }

  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/bin/bash", "-c"]
    command     = self.triggers.script
  }
}

# --- NodePools + EC2NodeClasses (charts/karpenter) ---
#
# First-party local chart: one helm_release → all NodePools install/uninstall
# atomically, which is what makes the destroy drain a single step. Values
# injected from Terraform. The gpu-g NodePool + GPU EC2NodeClass are always
# present (GPUs are mandatory for inference); the device plugin is nvcr.io-only
# and vendored separately (platform_gpu.tf).
resource "helm_release" "karpenter_nodepools" {
  name      = "karpenter-nodepools"
  chart     = "${path.module}/../charts/karpenter"
  namespace = local.karpenter_namespace

  # wait=false is load-bearing for DESTROY. This chart owns the NodePool +
  # EC2NodeClass CRs, which carry Karpenter finalizers that block deletion until
  # every NodeClaim (node) is drained. With the provider's default wait=true, the
  # uninstall BLOCKS on those finalizers and hits the helm timeout (300s) long
  # before a GPU node drains — failing `jd down` before null_resource.karpenter_drain
  # (destroy order: this release first, then the poller) ever runs. wait=false lets
  # uninstall issue the CR deletes and return; the CRs go Terminating and the drain
  # poller (controller still alive) owns the drain with its 600s window + EC2 force
  # path. The poller — not this uninstall — is the single thing that blocks on drain.
  # `wait` is shared across install/upgrade/uninstall, but this is a CR-only chart
  # (NodePool + EC2NodeClass) and helm's wait gates only built-in workload kinds, never
  # CRs — so wait=false is a no-op on apply and only changes the destroy path.
  wait = false

  set = [
    { name = "clusterName", value = module.eks_cluster.cluster_name },
    { name = "discoveryTag", value = local.cluster_name },
    { name = "nodeInstanceProfile", value = aws_iam_instance_profile.node.name },
    # GPU AMI id resolved from SSM (no alias exists for the AL2023 NVIDIA AMI).
    { name = "gpuAmiId", value = data.aws_ssm_parameter.gpu_ami.value },
    # High-end P pool (gated). Cost-safe by default — the CR is inert until a pod
    # opts in (nvidia-p label + taint). ODCR id pins it to a reservation if set.
    { name = "gpuP.enabled", value = tostring(var.enable_gpu_p_nodepool) },
    { name = "gpuP.capacityReservationId", value = var.gpu_p_capacity_reservation_id },
    # Capacity caps → NodePool spec.limits. These SAME values derive the Kueue
    # flavor nominalQuota (platform_kueue.tf), so admission never exceeds capacity.
    { name = "cpu.cpuLimit", value = tostring(var.cpu_capacity) },
    { name = "cpu.memoryLimit", value = var.memory_capacity },
    { name = "gpuG.gpuLimit", value = tostring(var.gpu_g_capacity) },
    { name = "gpuP.gpuLimit", value = tostring(var.gpu_p_capacity) },
    # GPU-node parallel image pull (containerd 2.2), gpu/gpu-p userData only.
    # On/off only; concurrency values are fixed defaults in the chart.
    { name = "gpuParallelPull.enabled", value = tostring(var.gpu_parallel_image_pull) },
    # Chart content hash so editing a chart file triggers a re-apply (see main.tf).
    { name = "chartContentHash", value = local.chart_hashes["karpenter"] },
  ]

  # NodePools require the Karpenter CRDs (installed by the controller release) and
  # the drain poller so destroy deletes NodePools -> drains -> then controller.
  depends_on = [
    helm_release.karpenter,
    null_resource.karpenter_drain,
  ]
}

# --- metrics-server: HPA + kubectl top ---
#
# registry.k8s.io image, explicitly pinned to the pull-through URI. On the
# tainted system NG (control-loop component).
resource "helm_release" "metrics_server" {
  name       = "metrics-server"
  repository = "https://kubernetes-sigs.github.io/metrics-server/"
  chart      = "metrics-server"
  version    = var.metrics_server_chart_version
  namespace  = "kube-system"

  set = [
    {
      name  = "image.repository"
      value = "${local.ecr_registry}/registry-k8s/metrics-server/metrics-server"
    },
    { name = "nodeSelector.inference/role", value = "system" },
    { name = "tolerations[0].key", value = "inference/role" },
    { name = "tolerations[0].operator", value = "Equal" },
    { name = "tolerations[0].value", value = "system" },
    { name = "tolerations[0].effect", value = "NoSchedule" },
  ]

  depends_on = [null_resource.cluster_addons, null_resource.pullthrough_ready, module.node_group]
}
