resource "kubernetes_manifest" "gpu_node_class" {
  manifest = {
    apiVersion = "karpenter.k8s.aws/v1"
    kind       = "EC2NodeClass"
    metadata = {
      name = "inference-multinode"
    }
    spec = {
      amiSelectorTerms = [{
        alias = "al2023@latest"
      }]
      role            = var.karpenter_node_role
      subnetSelector  = var.subnet_selector
      securityGroupSelector = var.security_group_selector
      blockDeviceMappings = [{
        deviceName = "/dev/xvda"
        ebs = {
          volumeSize = var.root_volume_size
          volumeType = "gp3"
          encrypted  = true
        }
      }]
      tags = merge(var.node_tags, {
        "inference/multinode" = "true"
      })
    }
  }
}

resource "kubernetes_manifest" "gpu_node_pool" {
  manifest = {
    apiVersion = "karpenter.sh/v1"
    kind       = "NodePool"
    metadata = {
      name = "inference-multinode"
    }
    spec = {
      template = {
        metadata = {
          labels = {
            "inference/node-type"  = "multinode-gpu"
            "inference/efa"        = "true"
          }
        }
        spec = {
          nodeClassRef = {
            group = "karpenter.k8s.aws"
            kind  = "EC2NodeClass"
            name  = "inference-multinode"
          }
          requirements = [
            {
              key      = "karpenter.k8s.aws/instance-family"
              operator = "In"
              values   = var.instance_families
            },
            {
              key      = "karpenter.sh/capacity-type"
              operator = "In"
              values   = var.capacity_types
            },
            {
              key      = "kubernetes.io/arch"
              operator = "In"
              values   = ["amd64"]
            }
          ]
          taints = [{
            key    = "nvidia.com/gpu"
            effect = "NoSchedule"
          }]
        }
      }
      disruption = {
        # WhenEmpty prevents Karpenter from consolidating nodes that are part
        # of an active gang-scheduled group. Nodes only become consolidation
        # candidates when all pods have been removed (full group termination).
        consolidationPolicy = "WhenEmpty"
        consolidateAfter    = var.consolidate_after
      }
      limits = {
        "nvidia.com/gpu" = var.gpu_limit
      }
    }
  }

  depends_on = [kubernetes_manifest.gpu_node_class]
}
