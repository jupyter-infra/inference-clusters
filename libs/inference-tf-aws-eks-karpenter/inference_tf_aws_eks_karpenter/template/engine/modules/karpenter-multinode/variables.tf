variable "karpenter_node_role" {
  description = "IAM role name for Karpenter-managed nodes."
  type        = string
}

variable "subnet_selector" {
  description = "Subnet selector tags for node placement."
  type        = map(string)
}

variable "security_group_selector" {
  description = "Security group selector tags for nodes."
  type        = map(string)
}

variable "instance_families" {
  description = "Allowed EC2 instance families for multi-node GPU nodes (e.g. p5, p4d, p5e)."
  type        = list(string)
}

variable "capacity_types" {
  description = "Allowed capacity types: on-demand, spot, or capacity-block."
  type        = list(string)
}

variable "gpu_limit" {
  description = "Maximum total GPUs this NodePool can provision."
  type        = number
}

variable "consolidate_after" {
  description = "Duration after which empty nodes are consolidated (e.g. '60s')."
  type        = string
}

variable "root_volume_size" {
  description = "Root EBS volume size for GPU nodes (e.g. '500Gi')."
  type        = string
}

variable "node_tags" {
  description = "Additional tags applied to provisioned nodes."
  type        = map(string)
}
