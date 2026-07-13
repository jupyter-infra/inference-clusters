# === Cluster Autoscaler ===
#
# Karpenter scales the INFERENCE nodes; nothing scales the system (platform) MNG,
# which without this sits pinned at bootstrap_desired_size. Cluster Autoscaler (CA)
# moves the system MNG's desired_size within bootstrap_min/max on Pending platform
# pods. It discovers ONLY the tagged system ASG via auto-discovery; Karpenter's
# inference nodes are non-ASG and untagged, so CA never touches them.
#
# Image: registry.k8s.io/autoscaling/cluster-autoscaler is on a no-creds pull-through
# upstream (registry-k8s), so it needs NO vendoring — pinned to the pull-through URI
# like metrics-server/KRO. On the tainted system NG (control-loop component).

locals {
  cluster_autoscaler_namespace       = "kube-system"
  cluster_autoscaler_service_account = "cluster-autoscaler"

  # CA's image tag tracks the cluster's Kubernetes MINOR version (one image per minor);
  # the .0 patch is the registry.k8s.io convention. The chart version is decoupled and
  # set via var.cluster_autoscaler_chart_version.
  cluster_autoscaler_image_tag = "v${var.kubernetes_version}.0"
}

# --- ASG discovery tags (THE propagation fix) ---
#
# EKS MNG `tags` do NOT propagate to the underlying ASG, and CA auto-discovery
# (--node-group-auto-discovery=asg:tag=...) reads ASG tags. So the discovery tags on
# module.node_group's aws_eks_node_group are inert for discovery. Attach them DIRECTLY
# to the ASG EKS created for the MNG — this, not the MNG tags, is what CA sees.
resource "aws_autoscaling_group_tag" "ca_enabled" {
  autoscaling_group_name = module.node_group.autoscaling_group_name
  tag {
    key                 = "k8s.io/cluster-autoscaler/enabled"
    value               = "true"
    propagate_at_launch = false
  }
}

resource "aws_autoscaling_group_tag" "ca_owned" {
  autoscaling_group_name = module.node_group.autoscaling_group_name
  tag {
    key                 = "k8s.io/cluster-autoscaler/${local.cluster_name}"
    value               = "owned"
    propagate_at_launch = false
  }
}

# --- CA controller role (Pod Identity) ---
#
# Pod Identity trust (shared doc in iam.tf). Policy transcribed from the upstream
# cluster-autoscaler cloudprovider/aws README (least-privilege variant): read actions
# are unscoped (Describe* has no resource-level scoping), the three MUTATING autoscaling
# actions are scoped to THIS cluster's ASGs via the k8s.io/cluster-autoscaler/<cluster>
# = owned ResourceTag condition (the tag the aws_autoscaling_group_tag resources set).
data "aws_iam_policy_document" "cluster_autoscaler" {
  statement {
    sid    = "AllowASGReadActions"
    effect = "Allow"
    actions = [
      "autoscaling:DescribeAutoScalingGroups",
      "autoscaling:DescribeAutoScalingInstances",
      "autoscaling:DescribeLaunchConfigurations",
      "autoscaling:DescribeScalingActivities",
      "autoscaling:DescribeTags",
      "ec2:DescribeImages",
      "ec2:DescribeInstanceTypes",
      "ec2:DescribeLaunchTemplateVersions",
      "ec2:GetInstanceTypesFromInstanceRequirements",
      "eks:DescribeNodegroup",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "AllowScopedASGWriteActions"
    effect = "Allow"
    actions = [
      "autoscaling:SetDesiredCapacity",
      "autoscaling:TerminateInstanceInAutoScalingGroup",
      "autoscaling:UpdateAutoScalingGroup",
    ]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/k8s.io/cluster-autoscaler/${local.cluster_name}"
      values   = ["owned"]
    }
  }
}

# The tag-scoped write statement needs a condition, so attach the policy document
# directly via aws_iam_role_policy (same pattern as the Karpenter controller role).
module "cluster_autoscaler_role" {
  source             = "./modules/iam_role"
  role_name          = "${local.resource_name_prefix}-cluster-autoscaler"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
  combined_tags      = local.combined_tags
}

resource "aws_iam_role_policy" "cluster_autoscaler" {
  name   = "${local.resource_name_prefix}-cluster-autoscaler"
  role   = module.cluster_autoscaler_role.role_name
  policy = data.aws_iam_policy_document.cluster_autoscaler.json
}

resource "aws_eks_pod_identity_association" "cluster_autoscaler" {
  cluster_name    = module.eks_cluster.cluster_name
  namespace       = local.cluster_autoscaler_namespace
  service_account = local.cluster_autoscaler_service_account
  role_arn        = module.cluster_autoscaler_role.role_arn
}

# --- CA controller (helm) ---

resource "helm_release" "cluster_autoscaler" {
  name       = "cluster-autoscaler"
  repository = "https://kubernetes.github.io/autoscaler"
  chart      = "cluster-autoscaler"
  version    = var.cluster_autoscaler_chart_version
  namespace  = local.cluster_autoscaler_namespace

  set = [
    # Image repin to the pull-through URI (PRIMARY resolution). Tag-only (no digest)
    # so pull-through import-on-miss fires. registry.k8s.io/autoscaling/cluster-autoscaler
    # -> <registry>/registry-k8s/autoscaling/cluster-autoscaler.
    {
      name  = "image.repository"
      value = "${local.ecr_registry}/registry-k8s/autoscaling/cluster-autoscaler"
    },
    {
      name  = "image.tag"
      value = local.cluster_autoscaler_image_tag
    },
    # Two replicas so a leader failover (node drain/consolidation) keeps a warm
    # standby; CA is leader-elected, so only one is active at a time.
    {
      name  = "replicaCount"
      value = "2"
    },
    # Auto-discovery of the tagged system ASG.
    {
      name  = "autoDiscovery.clusterName"
      value = module.eks_cluster.cluster_name
    },
    {
      name  = "awsRegion"
      value = var.region
    },
    # SA name MUST match the Pod Identity association above.
    {
      name  = "rbac.serviceAccount.name"
      value = local.cluster_autoscaler_service_account
    },
    {
      name  = "rbac.serviceAccount.create"
      value = "true"
    },
    # Balance node counts across similar node groups.
    {
      name  = "extraArgs.balance-similar-node-groups"
      value = "true"
    },
    # System NG placement (control-loop component).
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
    aws_eks_pod_identity_association.cluster_autoscaler,
    aws_autoscaling_group_tag.ca_enabled,
    aws_autoscaling_group_tag.ca_owned,
  ]
}
