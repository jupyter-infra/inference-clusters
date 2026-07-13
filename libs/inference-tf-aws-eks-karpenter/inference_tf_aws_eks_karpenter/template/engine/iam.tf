# --- Trust policies ---

data "aws_iam_policy_document" "eks_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "ec2_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "pod_identity_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

# --- Roles ---

module "cluster_role" {
  source             = "./modules/iam_role"
  role_name          = "${local.resource_name_prefix}-cluster"
  assume_role_policy = data.aws_iam_policy_document.eks_trust.json
  policy_arns        = ["arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonEKSClusterPolicy"]
  combined_tags      = local.combined_tags
}

# The bootstrap MNG nodes and (later) Karpenter-launched nodes share this one
# node role. AmazonEC2ContainerRegistryReadOnly grants the image pull; the
# pull-through import-on-miss inline policy is added later.
module "node_role" {
  source             = "./modules/iam_role"
  role_name          = "${local.resource_name_prefix}-node"
  assume_role_policy = data.aws_iam_policy_document.ec2_trust.json
  policy_arns = [
    "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
    "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore",
  ]
  combined_tags = local.combined_tags
}

module "ebs_csi_role" {
  source             = "./modules/iam_role"
  role_name          = "${local.resource_name_prefix}-ebs-csi"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
  policy_arns        = ["arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"]
  combined_tags      = local.combined_tags
}

# Instance profile for Karpenter-launched nodes, PRE-CREATED here (not by Karpenter).
#
# On the endpoints-only VPC Karpenter cannot reach IAM: IAM is a global
# service with NO regional VPC endpoint (nor does the pricing API). If the
# EC2NodeClass sets `role`, Karpenter manages the instance profile itself via
# CreateInstanceProfile/GetInstanceProfile/ListInstanceProfiles — all IAM calls that
# time out (dial tcp iam.amazonaws.com:443: i/o timeout). The reconcile then dies
# BEFORE writing Ready status, so every downstream controller misreports "no subnets
# found". Pre-creating the profile and setting `instanceProfile` on the EC2NodeClass
# makes Karpenter issue ZERO IAM calls.
resource "aws_iam_instance_profile" "node" {
  name = "${local.resource_name_prefix}-node"
  role = module.node_role.role_name
  tags = local.combined_tags
}

# --- Karpenter controller role ---
#
# Pod Identity trust; custom policy transcribed from Karpenter's published v1
# CloudFormation policy (aws/karpenter-provider-aws v1.13.0), scoped to this
# cluster via the kubernetes.io/cluster/<name> = owned tag condition. The
# controller provisions/terminates EC2 for inference NodePools only.
#
# No AWS-managed policy exists for the Karpenter controller (by design): every
# statement is scoped to THIS cluster's tag/queue, which a static managed policy
# can't embed — so the upstream policy ships as CloudFormation, transcribed here.
# (The node/cluster/CSI roles below DO use AWS-managed policies.)
locals {
  karpenter_queue_name    = local.cluster_name
  karpenter_queue_arn     = "arn:${data.aws_partition.current.partition}:sqs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:${local.karpenter_queue_name}"
  ec2_resource_arn_prefix = "arn:${data.aws_partition.current.partition}:ec2:${data.aws_region.current.id}"
}

data "aws_iam_policy_document" "karpenter_controller" {
  statement {
    sid     = "AllowScopedEC2InstanceAccessActions"
    actions = ["ec2:RunInstances", "ec2:CreateFleet"]
    resources = [
      "${local.ec2_resource_arn_prefix}::image/*",
      "${local.ec2_resource_arn_prefix}::snapshot/*",
      "${local.ec2_resource_arn_prefix}:${data.aws_caller_identity.current.account_id}:security-group/*",
      "${local.ec2_resource_arn_prefix}:${data.aws_caller_identity.current.account_id}:subnet/*",
      "${local.ec2_resource_arn_prefix}:${data.aws_caller_identity.current.account_id}:capacity-reservation/*",
      "${local.ec2_resource_arn_prefix}:${data.aws_caller_identity.current.account_id}:placement-group/*",
    ]
  }

  statement {
    sid       = "AllowScopedEC2LaunchTemplateAccessActions"
    actions   = ["ec2:RunInstances", "ec2:CreateFleet"]
    resources = ["${local.ec2_resource_arn_prefix}:${data.aws_caller_identity.current.account_id}:launch-template/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/kubernetes.io/cluster/${local.cluster_name}"
      values   = ["owned"]
    }
    condition {
      test     = "StringLike"
      variable = "aws:ResourceTag/karpenter.sh/nodepool"
      values   = ["*"]
    }
  }

  statement {
    sid     = "AllowScopedEC2InstanceActionsWithTags"
    actions = ["ec2:RunInstances", "ec2:CreateFleet", "ec2:CreateLaunchTemplate"]
    resources = [
      "${local.ec2_resource_arn_prefix}:${data.aws_caller_identity.current.account_id}:fleet/*",
      "${local.ec2_resource_arn_prefix}:${data.aws_caller_identity.current.account_id}:instance/*",
      "${local.ec2_resource_arn_prefix}:${data.aws_caller_identity.current.account_id}:volume/*",
      "${local.ec2_resource_arn_prefix}:${data.aws_caller_identity.current.account_id}:network-interface/*",
      "${local.ec2_resource_arn_prefix}:${data.aws_caller_identity.current.account_id}:launch-template/*",
      "${local.ec2_resource_arn_prefix}:${data.aws_caller_identity.current.account_id}:spot-instances-request/*",
    ]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/kubernetes.io/cluster/${local.cluster_name}"
      values   = ["owned"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/eks:eks-cluster-name"
      values   = [local.cluster_name]
    }
    condition {
      test     = "StringLike"
      variable = "aws:RequestTag/karpenter.sh/nodepool"
      values   = ["*"]
    }
  }

  statement {
    sid     = "AllowScopedResourceCreationTagging"
    actions = ["ec2:CreateTags"]
    resources = [
      "${local.ec2_resource_arn_prefix}:${data.aws_caller_identity.current.account_id}:fleet/*",
      "${local.ec2_resource_arn_prefix}:${data.aws_caller_identity.current.account_id}:instance/*",
      "${local.ec2_resource_arn_prefix}:${data.aws_caller_identity.current.account_id}:volume/*",
      "${local.ec2_resource_arn_prefix}:${data.aws_caller_identity.current.account_id}:network-interface/*",
      "${local.ec2_resource_arn_prefix}:${data.aws_caller_identity.current.account_id}:launch-template/*",
      "${local.ec2_resource_arn_prefix}:${data.aws_caller_identity.current.account_id}:spot-instances-request/*",
    ]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/kubernetes.io/cluster/${local.cluster_name}"
      values   = ["owned"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/eks:eks-cluster-name"
      values   = [local.cluster_name]
    }
    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values   = ["RunInstances", "CreateFleet", "CreateLaunchTemplate"]
    }
    condition {
      test     = "StringLike"
      variable = "aws:RequestTag/karpenter.sh/nodepool"
      values   = ["*"]
    }
  }

  statement {
    sid       = "AllowScopedResourceTagging"
    actions   = ["ec2:CreateTags"]
    resources = ["${local.ec2_resource_arn_prefix}:${data.aws_caller_identity.current.account_id}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/kubernetes.io/cluster/${local.cluster_name}"
      values   = ["owned"]
    }
    condition {
      test     = "StringLike"
      variable = "aws:ResourceTag/karpenter.sh/nodepool"
      values   = ["*"]
    }
    condition {
      test     = "StringEqualsIfExists"
      variable = "aws:RequestTag/eks:eks-cluster-name"
      values   = [local.cluster_name]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "aws:TagKeys"
      values   = ["eks:eks-cluster-name", "karpenter.sh/nodeclaim", "Name"]
    }
  }

  statement {
    sid     = "AllowScopedDeletion"
    actions = ["ec2:TerminateInstances", "ec2:DeleteLaunchTemplate"]
    resources = [
      "${local.ec2_resource_arn_prefix}:${data.aws_caller_identity.current.account_id}:instance/*",
      "${local.ec2_resource_arn_prefix}:${data.aws_caller_identity.current.account_id}:launch-template/*",
    ]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/kubernetes.io/cluster/${local.cluster_name}"
      values   = ["owned"]
    }
    condition {
      test     = "StringLike"
      variable = "aws:ResourceTag/karpenter.sh/nodepool"
      values   = ["*"]
    }
  }

  statement {
    sid = "AllowRegionalReadActions"
    actions = [
      "ec2:DescribeCapacityReservations",
      "ec2:DescribeImages",
      "ec2:DescribeInstances",
      "ec2:DescribeInstanceStatus",
      "ec2:DescribeInstanceTypeOfferings",
      "ec2:DescribeInstanceTypes",
      "ec2:DescribeLaunchTemplates",
      "ec2:DescribePlacementGroups",
      "ec2:DescribeSecurityGroups",
      "ec2:DescribeSpotPriceHistory",
      "ec2:DescribeSubnets",
    ]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [data.aws_region.current.id]
    }
  }

  statement {
    sid       = "AllowSSMReadActions"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:${data.aws_partition.current.partition}:ssm:${data.aws_region.current.id}::parameter/aws/service/*"]
  }

  statement {
    sid       = "AllowPricingReadActions"
    actions   = ["pricing:GetProducts"]
    resources = ["*"]
  }

  statement {
    sid       = "AllowInterruptionQueueActions"
    actions   = ["sqs:DeleteMessage", "sqs:GetQueueUrl", "sqs:ReceiveMessage"]
    resources = [local.karpenter_queue_arn]
  }

  # iam:PassRole is retained (needed to launch an instance WITH the pre-created
  # instance profile), but note it is evaluated server-side by EC2 during
  # RunInstances — it is NOT an outbound IAM call from Karpenter, so it does not hit
  # the missing-IAM-endpoint timeout. All instance-profile MANAGEMENT statements
  # (Create/Tag/AddRole/Get) were removed: with a pre-created instanceProfile on the
  # EC2NodeClass, Karpenter never manages profiles and never calls IAM
  # (air-gapped)— see aws_iam_instance_profile.node.
  statement {
    sid       = "AllowPassingInstanceRole"
    actions   = ["iam:PassRole"]
    resources = [module.node_role.role_arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ec2.amazonaws.com"]
    }
  }

  statement {
    sid       = "AllowAPIServerEndpointDiscovery"
    actions   = ["eks:DescribeCluster"]
    resources = ["arn:${data.aws_partition.current.partition}:eks:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:cluster/${local.cluster_name}"]
  }
}

# Karpenter's policy is tag-scoped (every statement carries conditions), so attach the
# policy document directly via aws_iam_role_policy rather than through a generic module.
module "karpenter_controller_role" {
  source             = "./modules/iam_role"
  role_name          = "${local.resource_name_prefix}-karpenter"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
  combined_tags      = local.combined_tags
}

resource "aws_iam_role_policy" "karpenter_controller" {
  name   = "${local.resource_name_prefix}-karpenter-controller"
  role   = module.karpenter_controller_role.role_name
  policy = data.aws_iam_policy_document.karpenter_controller.json
}
