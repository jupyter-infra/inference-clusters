output "node_group_name" {
  value = aws_eks_node_group.this.node_group_name
}

output "ami_type" {
  value = var.ami_type
}

# EKS creates and owns the ASG behind the MNG. Cluster Autoscaler discovers node
# groups by ASG tag, but MNG `tags` do NOT propagate to the underlying ASG — so the
# root must tag the ASG directly (aws_autoscaling_group_tag), and it needs the name.
# resources[0].autoscaling_groups[0] is populated once the MNG exists.
output "autoscaling_group_name" {
  value = aws_eks_node_group.this.resources[0].autoscaling_groups[0].name
}
