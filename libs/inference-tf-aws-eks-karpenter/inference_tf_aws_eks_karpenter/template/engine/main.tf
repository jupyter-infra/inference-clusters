terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = ">= 2.14"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.30"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.0"
    }
    time = {
      source  = "hashicorp/time"
      version = ">= 0.9"
    }
  }
}

provider "aws" {
  region = var.region
}

provider "kubernetes" {
  host                   = module.eks_cluster.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks_cluster.cluster_ca_certificate)
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks_cluster.cluster_name, "--region", var.region]
  }
}

provider "helm" {
  kubernetes = {
    host                   = module.eks_cluster.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks_cluster.cluster_ca_certificate)
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", module.eks_cluster.cluster_name, "--region", var.region]
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

resource "random_id" "postfix" {
  byte_length = 4
}

locals {
  template_name    = "tf-aws-eks-karpenter"
  template_version = "0.1.0rc1"

  # Deployment-independent identity tags applied to every resource. Source = jupyter-deploy
  # attributes the resource to the tooling, consistent across all jupyter-deploy templates.
  shared_tags = {
    Source   = "jupyter-deploy"
    Template = local.template_name
    Version  = local.template_version
  }

  default_tags = merge(
    local.shared_tags,
    {
      DeploymentId = random_id.postfix.hex
    },
    var.custom_tags,
  )
  combined_tags        = local.default_tags
  cluster_name         = "${var.cluster_name_prefix}-${random_id.postfix.hex}"
  resource_name_prefix = local.cluster_name
}

locals {
  # Content hash per first-party local chart (charts/*). The helm provider keys a
  # release on its `set` values + chart `version`, NOT on the chart directory's file
  # contents — so editing a chart template/values file produces NO plan diff and the
  # OLD render stays deployed (diagnosed live). Injecting this hash as a `set` value
  # on each local-chart helm_release makes any file change flip a tracked input →
  # Terraform plans the upgrade. The chart never reads the value; its presence is what
  # matters. Content-only hash (each file's sha256, ordered by path) — deterministic
  # across environments, unlike archive_file md5 (same idiom as eks-oidc's application
  # module). fileset("**") walks recursively; sort() keeps the join order stable.
  chart_dirs = ["karpenter", "kro", "kueue", "metrics", "storage", "inference-extension"]
  chart_hashes = {
    for name in local.chart_dirs :
    name => sha256(join("", [
      for f in sort(fileset("${path.module}/../charts/${name}", "**")) :
      filesha256("${path.module}/../charts/${name}/${f}")
    ]))
  }
}

module "vpc" {
  source               = "./modules/vpc"
  resource_name_prefix = local.resource_name_prefix
  cluster_name         = local.cluster_name
  enable_nat_gateway   = var.enable_nat_gateway
  combined_tags        = local.combined_tags
}

module "eks_cluster" {
  source                     = "./modules/eks_cluster"
  cluster_name               = local.cluster_name
  kubernetes_version         = var.kubernetes_version
  cluster_role_arn           = module.cluster_role.role_arn
  cluster_log_retention_days = var.cluster_log_retention_days
  vpc_id                     = module.vpc.vpc_id
  private_subnet_ids         = module.vpc.private_subnet_ids
  public_subnet_ids          = module.vpc.public_subnet_ids
  # Open by default — the endpoint is a knock-surface, not an auth boundary; EKS
  # access entries (IAM) are the real gate and the data plane stays internal
  # Not exposed as a variable for the POC.
  public_access_cidrs = ["0.0.0.0/0"]
  combined_tags       = local.combined_tags

  # Apply: VPC -> post-EKS cleanup sentinel -> EKS.
  # Destroy: EKS (and its managed node group) -> sentinel -> VPC.
  # This lets the sentinel reap CNI ENIs that detach only after the managed node
  # group is gone, before Terraform starts deleting subnets and the VPC.
  depends_on = [null_resource.post_eks_vpc_cleanup]
}

# Tag the cluster security group for Karpenter discovery (EC2NodeClass
# securityGroupSelectorTerms match karpenter.sh/discovery = cluster_name). The
# private subnets are tagged inside the vpc module; the SG is created by EKS, so
# it is tagged here.
resource "aws_ec2_tag" "cluster_sg_discovery" {
  resource_id = module.eks_cluster.cluster_security_group_id
  key         = "karpenter.sh/discovery"
  value       = local.cluster_name
}

# --- Access entries: the real authorization gate ---
#
# Caller identity detection: the deploying caller is dynamically merged into the
# admin_role_names or admin_user_names set so that switching callers produces no
# state diff (as long as all callers are declared in the appropriate list).
locals {
  caller_is_user   = can(regex(":user/", data.aws_caller_identity.current.arn))
  caller_role_name = !local.caller_is_user ? element(split("/", data.aws_caller_identity.current.arn), 1) : ""
  caller_user_name = local.caller_is_user ? regex(":user/(.+)$", data.aws_caller_identity.current.arn)[0] : ""

  all_admin_role_names = toset(
    !local.caller_is_user
    ? distinct(concat(var.admin_role_names, [local.caller_role_name]))
    : var.admin_role_names
  )
  all_admin_user_names = toset(
    local.caller_is_user
    ? distinct(concat(var.admin_user_names, [local.caller_user_name]))
    : var.admin_user_names
  )
}

data "aws_iam_role" "admin" {
  for_each = local.all_admin_role_names
  name     = each.value
}

data "aws_iam_user" "admin" {
  for_each  = local.all_admin_user_names
  user_name = each.value
}

resource "aws_eks_access_entry" "admin_role" {
  for_each          = data.aws_iam_role.admin
  cluster_name      = module.eks_cluster.cluster_name
  principal_arn     = each.value.arn
  kubernetes_groups = ["cluster-admin-group"]
}

resource "aws_eks_access_policy_association" "admin_role" {
  for_each      = data.aws_iam_role.admin
  cluster_name  = module.eks_cluster.cluster_name
  principal_arn = each.value.arn
  policy_arn    = "arn:${data.aws_partition.current.partition}:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_access_entry.admin_role]
}

resource "aws_eks_access_entry" "admin_user" {
  for_each          = data.aws_iam_user.admin
  cluster_name      = module.eks_cluster.cluster_name
  principal_arn     = each.value.arn
  kubernetes_groups = ["cluster-admin-group"]
}

resource "aws_eks_access_policy_association" "admin_user" {
  for_each      = data.aws_iam_user.admin
  cluster_name  = module.eks_cluster.cluster_name
  principal_arn = each.value.arn
  policy_arn    = "arn:${data.aws_partition.current.partition}:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_access_entry.admin_user]
}

# Node-role access entry (EC2_LINUX): required with API auth mode so both the
# bootstrap MNG nodes and (later) Karpenter-launched nodes can join the cluster.
resource "aws_eks_access_entry" "node" {
  cluster_name  = module.eks_cluster.cluster_name
  principal_arn = module.node_role.role_arn
  type          = "EC2_LINUX"
}

# --- System managed node group ---
#
# AMI type is resolved HERE at the root, not inside the node_group module: a data
# source inside a module inherits that module's `depends_on`, so it would be
# deferred to apply-time whenever an upstream dep has a pending change, making
# ami_type "known after apply" and FORCING a node group replacement on every
# re-apply. At the root there is no depends_on, so this stays plan-time-stable.
data "aws_ec2_instance_type" "bootstrap" {
  instance_type = var.bootstrap_instance_types[0]
}

locals {
  bootstrap_has_gpu    = try(length(data.aws_ec2_instance_type.bootstrap.gpus) > 0, false)
  bootstrap_has_neuron = try(length(data.aws_ec2_instance_type.bootstrap.neuron_devices) > 0, false)
  bootstrap_arch       = contains(try(data.aws_ec2_instance_type.bootstrap.supported_architectures, ["x86_64"]), "x86_64") ? "x86_64" : "arm64"

  bootstrap_ami_type = (
    local.bootstrap_has_gpu && local.bootstrap_arch == "x86_64" ? "AL2023_x86_64_NVIDIA" :
    local.bootstrap_has_neuron ? "AL2023_x86_64_NEURON" :
    local.bootstrap_arch == "arm64" ? "AL2023_ARM_64_STANDARD" :
    "AL2023_x86_64_STANDARD"
  )
}

# Tainted so ONLY control-loop pods (which we own and give the toleration) land
# here; the label lets those pods target it via nodeSelector. Karpenter-launched
# inference nodes carry neither, keeping the two node populations disjoint.
module "node_group" {
  source = "./modules/node_group"

  cluster_name = module.eks_cluster.cluster_name
  # Static "platform" (not "${cluster_name}-system") so `jd pool list` reads cleanly:
  # node group names are unique per-CLUSTER, not per-account (ARN embeds the cluster +
  # a uuid), so two stacks each having a "platform" node group is safe. The MNG carries
  # the inference/role=system taint/label regardless of its EKS-facing name.
  node_group_name = "platform"
  node_role_arn   = module.node_role.role_arn
  subnet_ids      = module.vpc.private_subnet_ids
  instance_types  = var.bootstrap_instance_types
  ami_type        = local.bootstrap_ami_type

  labels = {
    "inference/role" = "system"
  }
  taints = [{
    key    = "inference/role"
    value  = "system"
    effect = "NO_SCHEDULE"
  }]

  disk_size_gb = 50
  min_size     = var.bootstrap_min_size
  max_size     = var.bootstrap_max_size
  desired_size = var.bootstrap_desired_size

  # Containerd pull-through mirror (backup): redirect trusted upstreams to
  # our ECR pull-through repos. Primary resolution is explicit image pinning in
  # platform chart values.
  ecr_registry = local.ecr_registry
  mirror_map   = { for k, u in local.trusted_upstreams : u.url => u.prefix }

  combined_tags = merge(local.combined_tags, {
    # Cluster Autoscaler discovers ONLY this ASG via these tags; it never
    # touches Karpenter's (untagged, non-ASG) inference nodes.
    "k8s.io/cluster-autoscaler/enabled"               = "true"
    "k8s.io/cluster-autoscaler/${local.cluster_name}" = "owned"
  })

  # aws_eks_access_entry.node: with API auth mode this EC2_LINUX entry authorizes the
  # node role to JOIN. Without this edge the node group and the entry are siblings, so
  # Terraform may create nodes before the entry exists → they never register and the
  # node group fails with NodeCreationFailure ("new nodes are not joining").
  # core_node_addons: create after CNI/kube-proxy so nodes join a functional
  # network; on destroy, nodes drain BEFORE those DaemonSet addons are removed.
  # pullthrough_ready: the node's containerd mirror + import IAM must exist before
  # the node boots, or a mirror-redirected pull fails closed.
  depends_on = [
    aws_eks_access_entry.node,
    null_resource.core_node_addons,
    null_resource.pullthrough_ready,
  ]
}
