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
  }
}

provider "aws" {
  region = var.region
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

  default_tags = merge(
    {
      Source       = "inference"
      Template     = local.template_name
      Version      = local.template_version
      DeploymentId = random_id.postfix.hex
    },
    var.custom_tags,
  )

  doc_postfix = random_id.postfix.hex
}

# NOTE: seed scaffold. VPC, EKS cluster, Karpenter install, and self-managed
# NodePools/EC2NodeClasses are added in follow-up commits.

# --- Multi-Node Inference Platform Modules ---
# Each module is gated behind an enable_* flag (default: false).
# Only clusters that opt into multi-node inference pay the cost.

module "lws" {
  count  = var.enable_lws ? 1 : 0
  source = "./modules/lws"

  lws_namespace = "lws-system"
  lws_version   = var.lws_version
}

module "kro" {
  count  = var.enable_kro ? 1 : 0
  source = "./modules/kro"

  kro_namespace = "kro-system"
  kro_version   = var.kro_version
}

module "kueue" {
  count  = var.enable_kueue ? 1 : 0
  source = "./modules/kueue"

  kueue_namespace    = "kueue-system"
  kueue_version      = var.kueue_version
  cluster_queue_name = var.kueue_cluster_queue_name
  cohort_name        = var.kueue_cohort_name
  gpu_quota          = var.kueue_gpu_quota
  gpu_lending_limit  = var.kueue_gpu_lending_limit
  cpu_quota          = var.kueue_cpu_quota
  memory_quota       = var.kueue_memory_quota
  workload_namespace = var.kueue_workload_namespace

  # Optional features
  enable_prometheus_metrics       = var.kueue_enable_prometheus
  enable_topology_aware_scheduling = var.kueue_enable_tas
  topology_levels                 = var.kueue_topology_levels
}

module "karpenter_multinode" {
  count  = var.enable_multinode_nodepool ? 1 : 0
  source = "./modules/karpenter-multinode"

  karpenter_node_role      = "${var.cluster_name_prefix}-${local.doc_postfix}-node"
  subnet_selector          = { "inference/cluster" = "${var.cluster_name_prefix}-${local.doc_postfix}" }
  security_group_selector  = { "inference/cluster" = "${var.cluster_name_prefix}-${local.doc_postfix}" }
  instance_families        = var.multinode_instance_families
  capacity_types           = var.multinode_capacity_types
  gpu_limit                = var.multinode_gpu_limit
  consolidate_after        = var.multinode_consolidate_after
  root_volume_size         = var.multinode_root_volume_size
  node_tags                = local.default_tags
}

module "nvidia_device_plugin" {
  count  = var.enable_nvidia_plugin ? 1 : 0
  source = "./modules/nvidia-device-plugin"

  nvidia_namespace      = "nvidia-device-plugin"
  nvidia_plugin_version = var.nvidia_plugin_version
  enable_nfd            = var.enable_nfd
}

module "efa_device_plugin" {
  count  = var.enable_efa_plugin ? 1 : 0
  source = "./modules/efa-device-plugin"

  efa_namespace      = "kube-system"
  efa_plugin_version = var.efa_plugin_version
}

module "efs_csi_driver" {
  count  = var.enable_efs_csi ? 1 : 0
  source = "./modules/efs-csi-driver"

  efs_namespace              = "kube-system"
  efs_csi_version            = var.efs_csi_version
  efs_csi_role_arn           = var.efs_csi_role_arn
  efs_filesystem_id          = var.efs_filesystem_id
  model_weights_storage_size = var.model_weights_storage_size
}
