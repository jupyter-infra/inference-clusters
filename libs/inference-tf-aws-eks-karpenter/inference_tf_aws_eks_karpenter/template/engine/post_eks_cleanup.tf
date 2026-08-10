# EKS and the VPC CNI create resources outside Terraform state. In particular,
# managed-node-group CNI ENIs can become available only after the node group is
# destroyed, which is later than the Karpenter drain hook. A lingering ENI keeps
# the EKS-created cluster security group in use and blocks VPC deletion.
#
# This sentinel is ordered between the complete VPC network and EKS:
#   apply:   VPC network -> sentinel -> EKS -> managed node group
#   destroy: managed node group -> EKS -> sentinel -> VPC network
#
# `network_barrier` is a load-bearing attribute reference. The subnet output has
# module-level depends_on edges to routing and endpoints, so retaining it here
# prevents Terraform from deleting VPC resources in parallel with this cleanup.
resource "null_resource" "post_eks_vpc_cleanup" {
  triggers = {
    cluster_name    = local.cluster_name
    region          = var.region
    vpc_id          = module.vpc.vpc_id
    network_barrier = join(",", module.vpc.private_subnet_ids)

    # Inline the script in state so destroy still works if the source template is
    # no longer present after an upgrade.
    script = templatefile("${path.module}/post-eks-vpc-cleanup.sh.tftpl", {
      cluster_name = local.cluster_name
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
