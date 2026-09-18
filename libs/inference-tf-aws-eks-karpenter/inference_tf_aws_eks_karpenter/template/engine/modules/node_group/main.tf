# ami_type resolves from the ROOT (var.ami_type is concrete, never "default" here).
# Setting it here would cause the derived ami_type in downstream module to be 
# "known after apply", FORCING a full node group replacement at each `jd up`.

# Reason: a data source inside a module inherits the module's `depends_on`, which
# causes the read action to defer data.aws_ec2_instance_type` to apply-time
# ("read during apply") whenever ANY module dependency (core_node_addons, pullthrough_ready)
# has a pending change.

# Launch template carries the containerd pull-through mirror userData (backup
# mechanism). No AMI ID is set, so EKS still owns the AL2023 AMI AND
# merges the cluster-join NodeConfig with our partial NodeConfig — we supply only
# the containerd config + hosts.toml shell part.

# disk_size declared here because the node group config prohibits it
# once a launch template is attached.
resource "aws_launch_template" "this" {
  name_prefix = "${var.node_group_name}-"

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size = var.disk_size_gb
      volume_type = "gp3"
      encrypted   = true
    }
  }

  # IMDSv2 with hop limit 2 (required default for MNG custom launch templates).
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  # --- containerd registry-mirror userData: redirect upstream pulls to ECR pull-through ---
  #
  # On the endpoints-only VPC a node has no public egress, so a bare `docker.io/...` or
  # `quay.io/...` pull cannot resolve. This userData configures containerd to redirect
  # pulls of trusted upstream hosts to this account's ECR pull-through cache instead. It
  # is a BEST-EFFORT BACKUP: primary resolution is explicit pull-through image pins in
  # chart values; this only catches a bare upstream ref that slips through. The rendered
  # userdata.sh.tftpl is a two-part MIME document (how AL2023 / nodeadm consumes userData):
  #
  #  1. NodeConfig part (application/node.eks.aws) — sets containerd's
  #     `config_path = "/etc/containerd/certs.d"`, which ENABLES the hosts.toml mechanism.
  #     Despite the "certs.d" name this is the registry HOSTS-config dir (mirror/host
  #     redirection), NOT TLS certs — containerd only falls back to cert files there if no
  #     hosts.toml exists. We write no cert config. EKS injects the cluster-join NodeConfig
  #     as a separate MIME part and nodeadm merges the two, so we supply only this partial.
  #  2. Shell part — writes one /etc/containerd/certs.d/<host>/hosts.toml per upstream:
  #         server = "https://<host>"                          # real upstream, used as fallback
  #         [host."https://<ecr_registry>/v2/<prefix>"]        # our ECR cache = the mirror
  #           capabilities = ["pull", "resolve"]               # resolve = trusted tag->digest
  #           override_path = true
  #
  # Why override_path is load-bearing: ECR pull-through NESTS the upstream repo under a
  # prefix segment — e.g. quay.io/prometheus/node-exporter is pulled as
  # <account>.dkr.ecr.<region>.amazonaws.com/quay/prometheus/node-exporter. Containerd
  # normally treats a mirror as a bare HOST and auto-appends `/v2/<image>`, which would drop
  # our `/v2/<prefix>` path. override_path = true tells containerd the API root is defined by
  # the URL path and to use it verbatim, preserving the prefix. `server` is only a fallback
  # (tried after the mirror), and on this VPC it is unreachable — the mirror must hit.
  #
  # Refs:
  #  - containerd hosts.toml (config_path, server, [host], capabilities, override_path):
  #    https://github.com/containerd/containerd/blob/main/docs/hosts.md
  #  - ECR pull-through cache URI / prefix nesting:
  #    https://docs.aws.amazon.com/AmazonECR/latest/userguide/pull-through-cache-working.html
  #  - EKS AL2023 nodeadm NodeConfig userData (MIME multipart, NodeConfig schema):
  #    https://awslabs.github.io/amazon-eks-ami/nodeadm/
  #    (the spec.containerd.config field is in the linked API reference)
  #
  # NOTE: this backup applies to THIS system MNG only. Karpenter-launched inference nodes
  # use the EC2NodeClass (charts/karpenter), which carries no hosts.toml mirror — they rely
  # entirely on the primary explicit-pin path.
  user_data = base64encode(templatefile("${path.module}/userdata.sh.tftpl", {
    ecr_registry = var.ecr_registry
    mirror_map   = var.mirror_map
  }))

  tag_specifications {
    resource_type = "instance"
    tags          = merge(var.combined_tags, { Name = var.node_group_name })
  }

  tags = var.combined_tags

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_eks_node_group" "this" {
  cluster_name    = var.cluster_name
  node_group_name = var.node_group_name
  node_role_arn   = var.node_role_arn
  subnet_ids      = var.subnet_ids
  ami_type        = var.ami_type

  instance_types = var.instance_types

  launch_template {
    id      = aws_launch_template.this.id
    version = aws_launch_template.this.latest_version
  }

  labels = var.labels

  dynamic "taint" {
    for_each = var.taints
    content {
      key    = taint.value.key
      value  = taint.value.value
      effect = taint.value.effect
    }
  }

  scaling_config {
    min_size     = var.min_size
    max_size     = var.max_size
    desired_size = var.desired_size
  }

  tags = var.combined_tags

  # Cluster Autoscaler moves desired_size within min/max; ignore it
  # so a subsequent apply doesn't fight CA by resetting the count.
  lifecycle {
    ignore_changes = [scaling_config[0].desired_size]
  }
}
