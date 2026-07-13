resource "time_sleep" "wait_for_nodes" {
  create_duration = "30s"
  depends_on      = [module.node_group]
}

locals {
  # The system NG is tainted inference/role=system:NoSchedule AND labeled
  # inference/role=system. Every controller Deployment we place there needs BOTH:
  #   - the toleration, to get PAST the taint, and
  #   - the nodeSelector, to be PINNED to the system NG — a toleration alone only
  #     PERMITS the system NG, it doesn't prevent the pod from landing on some other
  #     untainted node (e.g. a future Karpenter CPU pool). The nodeSelector is what
  #     keeps addon controllers off Karpenter nodes for good.
  # DaemonSet parts (vpc-cni, kube-proxy, the ebs/s3 CSI node plugins, the CloudWatch
  # agent + Fluent Bit) are deliberately NOT pinned: they must run on EVERY node,
  # Karpenter GPU nodes included.
  system_toleration = {
    key      = "inference/role"
    operator = "Equal"
    value    = "system"
    effect   = "NoSchedule"
  }
}

# --- Addon ordering aggregators (eks-oidc pattern) ---

# DaemonSet addons a node needs to be functional: vpc-cni (pod IPs) and kube-proxy
# (Service/ClusterIP routing). The node group depends_on this, so on create the
# CNI is in place before nodes join, and on destroy the nodes drain BEFORE these
# are removed. Only DaemonSets belong here (they report healthy with zero nodes,
# so requiring them before the node group never deadlocks).
resource "null_resource" "core_node_addons" {
  depends_on = [
    aws_eks_addon.vpc_cni,
    aws_eks_addon.kube_proxy,
  ]
}

# Every cluster addon. Helm releases depend_on this, so on create all
# addons are up before any chart installs, and on destroy every chart uninstalls
# BEFORE any addon is removed (ebs-csi for PVC/PV teardown, coredns for in-cluster
# DNS, ...). Add a new addon here once and all charts inherit the ordering.
#
# The admin access-policy associations are also pulled in here: they are what
# authorize the Helm/Kubernetes providers to reach the cluster. On destroy (reverse
# order) every chart routes through this aggregator, so all charts uninstall BEFORE
# the association is torn down — otherwise the providers lose authorization mid-destroy
# and remaining uninstalls fail "forbidden". The node access entry is kept alive
# for the same reason: nodes must stay joined until charts are gone.
resource "null_resource" "cluster_addons" {
  depends_on = [
    aws_eks_addon.vpc_cni,
    aws_eks_addon.kube_proxy,
    aws_eks_addon.coredns,
    aws_eks_addon.pod_identity_agent,
    aws_eks_addon.ebs_csi_driver,
    aws_eks_addon.s3_csi_driver,
    aws_eks_addon.cw_observability,
    aws_eks_access_policy_association.admin_role,
    aws_eks_access_policy_association.admin_user,
    aws_eks_access_entry.node,
  ]
}

resource "aws_eks_addon" "vpc_cni" {
  cluster_name = module.eks_cluster.cluster_name
  addon_name   = "vpc-cni"
  tags         = local.combined_tags

  # Enable Kubernetes NetworkPolicy enforcement. The VPC CNI ships with the
  # network-policy agent DISABLED by default, which silently makes every
  # NetworkPolicy inert (nothing is blocked).
  configuration_values = jsonencode({
    enableNetworkPolicy = "true"
  })
}

resource "aws_eks_addon" "kube_proxy" {
  cluster_name = module.eks_cluster.cluster_name
  addon_name   = "kube-proxy"
  tags         = local.combined_tags
}

resource "aws_eks_addon" "coredns" {
  cluster_name = module.eks_cluster.cluster_name
  addon_name   = "coredns"
  tags         = local.combined_tags

  # coredns is a Deployment — pin it to the system NG (nodeSelector) and tolerate the taint.
  configuration_values = jsonencode({
    nodeSelector = local.system_node_selector
    tolerations  = [local.system_toleration]
  })

  depends_on = [time_sleep.wait_for_nodes]
}

resource "aws_eks_addon" "pod_identity_agent" {
  cluster_name = module.eks_cluster.cluster_name
  addon_name   = "eks-pod-identity-agent"
  tags         = local.combined_tags
}

resource "aws_eks_addon" "ebs_csi_driver" {
  cluster_name = module.eks_cluster.cluster_name
  addon_name   = "aws-ebs-csi-driver"
  tags         = local.combined_tags

  # The controller is a Deployment — pin to the system NG + tolerate its taint. The node
  # plugin is a DaemonSet that must tolerate all taints so it also runs on Karpenter nodes
  # (an EBS volume can be mounted by a workspace/GPU pod on any node).
  configuration_values = jsonencode({
    controller = {
      nodeSelector = local.system_node_selector
      tolerations  = [local.system_toleration]
    }
    node = {
      tolerateAllTaints = true
    }
  })

  pod_identity_association {
    role_arn        = module.ebs_csi_role.role_arn
    service_account = "ebs-csi-controller-sa"
  }

  depends_on = [aws_eks_addon.pod_identity_agent]
}

# Mountpoint-for-S3 CSI driver — mounts s3://<bucket>/models as a read-only POSIX
# path (the s3-models StorageClass, charts/storage). Authenticates via Pod Identity
# to the dedicated s3_csi_role (platform_storage.tf), NOT the node role. The
# node plugin is a DaemonSet that must tolerate all taints so mounts work on GPU /
# Karpenter nodes; the controller is a Deployment pinned (nodeSelector) to the system NG.
resource "aws_eks_addon" "s3_csi_driver" {
  cluster_name  = module.eks_cluster.cluster_name
  addon_name    = "aws-mountpoint-s3-csi-driver"
  addon_version = var.mountpoint_s3_csi_version
  tags          = local.combined_tags

  configuration_values = jsonencode({
    controller = {
      nodeSelector = local.system_node_selector
      tolerations  = [local.system_toleration]
    }
    node = {
      tolerateAllTaints = true
    }
  })

  pod_identity_association {
    role_arn        = module.s3_csi_role.role_arn
    service_account = "s3-csi-driver-sa"
  }

  depends_on = [aws_eks_addon.pod_identity_agent]
}

# CloudWatch Observability / Container Insights — cluster-wide by design: the CloudWatch
# agent + Fluent Bit DaemonSets tolerate ALL taints, so they collect metrics/logs from
# EVERY node (system MNG AND Karpenter GPU nodes). An EKS managed addon like the others
# above (AWS-managed image reached natively, no pull-through). Gated for cost.
module "cw_observability_role" {
  count = var.enable_container_insights ? 1 : 0

  source             = "./modules/iam_role"
  role_name          = "${local.resource_name_prefix}-cw-observability"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
  policy_arns        = ["arn:${data.aws_partition.current.partition}:iam::aws:policy/CloudWatchAgentServerPolicy"]
  combined_tags      = local.combined_tags
}

resource "aws_eks_addon" "cw_observability" {
  count = var.enable_container_insights ? 1 : 0

  cluster_name = module.eks_cluster.cluster_name
  addon_name   = "amazon-cloudwatch-observability"
  tags         = local.combined_tags

  # Two distinct placements:
  #  - top-level tolerations: the CloudWatch agent + Fluent Bit DaemonSets tolerate-all
  #    so they collect from EVERY node, incl. Karpenter GPU nodes.
  #  - manager.*: the operator Deployment (controller-manager) is a control-loop pod, so
  #    pin it to the tainted system NG — otherwise it lands on a Karpenter node
  #    and pins it from consolidating (confirmed manager.{nodeSelector,
  #    tolerations} in the v6 addon schema).
  configuration_values = jsonencode({
    tolerations = [
      { operator = "Exists" },
    ]
    manager = {
      nodeSelector = local.system_node_selector
      tolerations  = [local.system_toleration]
    }
  })

  pod_identity_association {
    role_arn        = module.cw_observability_role[0].role_arn
    service_account = "cloudwatch-agent"
  }

  depends_on = [aws_eks_addon.pod_identity_agent]
}
