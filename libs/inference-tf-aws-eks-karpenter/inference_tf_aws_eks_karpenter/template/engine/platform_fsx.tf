# === FSx for Lustre — high-throughput RWX file system (opt-in) ===
#
# The third weight-serving path alongside S3-direct and S3-mount (Mountpoint-for-S3).
# Backs workloads that need RWX POSIX with sub-ms metadata and hundreds of GB/s of
# aggregate throughput (LoRA/checkpoint scratch, shared dataset caches, KV-cache
# offload). Off by default: a PERSISTENT_2 SSD file system has a non-trivial hourly
# cost floor even when idle, and it is single-AZ (FSx for Lustre is one subnet).
#
# When enabled, this file provisions:
#   - a dedicated SG allowing Lustre TCP 988 + 1018-1023 in both directions between
#     the FSx ENIs and the EKS cluster SG (and self-referencing for inter-server RPC),
#   - a PERSISTENT_2 SSD file system with LZ4 compression, KMS at-rest encryption
#     (customer-managed if var.fsx_kms_key_arn is set, else the aws/fsx AWS-managed
#     key), WARN_ERROR event logs to CloudWatch,
#   - a Data Repository Association at /models linking to s3://<model_store>/models/
#     (auto-import all events, auto-export disabled — S3 is the source of truth),
#   - the aws-fsx-csi-driver Helm release (controller Deployment on the tainted
#     system NG, node plugin DaemonSet tolerates all taints), Pod Identity–bound
#     to a dedicated role with a least-privilege Describe-only inline policy.
#
# AZ: FSx lands in module.vpc.private_subnet_ids[0]. FSx-consumer pods must add a
# topology.kubernetes.io/zone nodeAffinity to output.fsx_availability_zone or accept
# cross-AZ mount latency + inter-AZ transfer. See research/fsx/terraform-eks-integration.md.

locals {
  fsx_namespace  = "kube-system"
  fsx_mount_path = "/models"
  fsx_subnet_id  = var.enable_fsx ? module.vpc.private_subnet_ids[0] : ""
}

# --- Service-linked role: NOT pre-created here (by design) ---
#
# AWSServiceRoleForAmazonFSx is an ACCOUNT-GLOBAL singleton. Pre-creating it via
# aws_iam_service_linked_role would collide when two deployments in one account both
# enable FSx (second apply fails "role has been taken"), and on destroy could yank it
# out from under a peer deployment's file system (jd down hangs). FSx auto-creates
# the SLR on the first CreateFileSystem call, so on a truly fresh account the very
# first apply MAY hit an InvalidServiceLinkedRole race — documented, easy to retry.
# Every subsequent apply (in this account or any other) is a no-op. This matches the
# same "shared account-regional singleton, not TF-managed" pattern pullthrough.tf uses
# for the ECR pull-through cache rule.

# --- Security group + rules (988 / 1018-1023 TCP, self + cluster SG) ---
#
# Sourced by SG reference (not CIDR) so EFA-enabled NodePools compose without a
# separate rule — CIDR-based rules do not satisfy EFA even at 0.0.0.0/0.
resource "aws_security_group" "fsx" {
  count       = var.enable_fsx ? 1 : 0
  name_prefix = "${local.resource_name_prefix}-fsx-"
  description = "FSx for Lustre file-system SG"
  vpc_id      = module.vpc.vpc_id
  tags        = merge(local.combined_tags, { Name = "${local.resource_name_prefix}-fsx" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "fsx_988_from_cluster" {
  count                        = var.enable_fsx ? 1 : 0
  security_group_id            = aws_security_group.fsx[0].id
  ip_protocol                  = "tcp"
  from_port                    = 988
  to_port                      = 988
  referenced_security_group_id = module.eks_cluster.cluster_security_group_id
  description                  = "Lustre RPC from EKS cluster SG"
  tags                         = local.combined_tags
}

resource "aws_vpc_security_group_ingress_rule" "fsx_1018_1023_from_cluster" {
  count                        = var.enable_fsx ? 1 : 0
  security_group_id            = aws_security_group.fsx[0].id
  ip_protocol                  = "tcp"
  from_port                    = 1018
  to_port                      = 1023
  referenced_security_group_id = module.eks_cluster.cluster_security_group_id
  description                  = "Lustre reserved range from EKS cluster SG"
  tags                         = local.combined_tags
}

resource "aws_vpc_security_group_ingress_rule" "fsx_988_self" {
  count                        = var.enable_fsx ? 1 : 0
  security_group_id            = aws_security_group.fsx[0].id
  ip_protocol                  = "tcp"
  from_port                    = 988
  to_port                      = 988
  referenced_security_group_id = aws_security_group.fsx[0].id
  description                  = "Lustre RPC self (inter-server)"
  tags                         = local.combined_tags
}

resource "aws_vpc_security_group_ingress_rule" "fsx_1018_1023_self" {
  count                        = var.enable_fsx ? 1 : 0
  security_group_id            = aws_security_group.fsx[0].id
  ip_protocol                  = "tcp"
  from_port                    = 1018
  to_port                      = 1023
  referenced_security_group_id = aws_security_group.fsx[0].id
  description                  = "Lustre reserved range self (inter-server)"
  tags                         = local.combined_tags
}

resource "aws_vpc_security_group_egress_rule" "fsx_all_to_cluster" {
  count                        = var.enable_fsx ? 1 : 0
  security_group_id            = aws_security_group.fsx[0].id
  ip_protocol                  = "-1"
  referenced_security_group_id = module.eks_cluster.cluster_security_group_id
  description                  = "Allow all egress to cluster SG"
  tags                         = local.combined_tags
}

resource "aws_vpc_security_group_egress_rule" "fsx_all_self" {
  count                        = var.enable_fsx ? 1 : 0
  security_group_id            = aws_security_group.fsx[0].id
  ip_protocol                  = "-1"
  referenced_security_group_id = aws_security_group.fsx[0].id
  description                  = "Allow all egress self"
  tags                         = local.combined_tags
}

# Client-side (EKS cluster SG) egress complement — the VPC CNI attaches this SG to
# every pod ENI, so this is the right client SG for Lustre traffic from pods.
resource "aws_vpc_security_group_egress_rule" "cluster_to_fsx_988" {
  count                        = var.enable_fsx ? 1 : 0
  security_group_id            = module.eks_cluster.cluster_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 988
  to_port                      = 988
  referenced_security_group_id = aws_security_group.fsx[0].id
  description                  = "Lustre RPC to FSx"
  tags                         = local.combined_tags
}

resource "aws_vpc_security_group_egress_rule" "cluster_to_fsx_1018_1023" {
  count                        = var.enable_fsx ? 1 : 0
  security_group_id            = module.eks_cluster.cluster_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 1018
  to_port                      = 1023
  referenced_security_group_id = aws_security_group.fsx[0].id
  description                  = "Lustre reserved range to FSx"
  tags                         = local.combined_tags
}

# --- CloudWatch log group for FSx event logs (WARN_ERROR) ---
#
# The log-group name MUST start with /aws/fsx/ (an FSx enforcement); retention
# mirrors cluster_log_retention_days for consistency with the rest of the stack.
resource "aws_cloudwatch_log_group" "fsx" {
  count             = var.enable_fsx ? 1 : 0
  name              = "/aws/fsx/${local.resource_name_prefix}"
  retention_in_days = var.cluster_log_retention_days
  tags              = local.combined_tags
}

# --- File system: PERSISTENT_2 SSD + LZ4 + DRA-capable ---
#
# storage_capacity × per_unit_storage_throughput / 1024 = aggregate MB/s.
# Both dials are in-place updatable on PERSISTENT_2 (UpdateFileSystem) — safe to
# start conservative and grow. Backups off: S3 (via DRA) is the durable copy.
resource "aws_fsx_lustre_file_system" "shared" {
  count = var.enable_fsx ? 1 : 0

  storage_type                = "SSD"
  deployment_type             = "PERSISTENT_2"
  storage_capacity            = var.fsx_storage_capacity_gib
  per_unit_storage_throughput = var.fsx_per_unit_storage_throughput
  data_compression_type       = "LZ4"
  file_system_type_version    = "2.15"
  kms_key_id                  = var.fsx_kms_key_arn == "" ? null : var.fsx_kms_key_arn

  subnet_ids         = [local.fsx_subnet_id]
  security_group_ids = [aws_security_group.fsx[0].id]

  weekly_maintenance_start_time   = "7:03:00"
  automatic_backup_retention_days = 0
  copy_tags_to_backups            = true

  log_configuration {
    level       = "WARN_ERROR"
    destination = aws_cloudwatch_log_group.fsx[0].arn
  }

  tags = merge(local.combined_tags, {
    Name = "${local.resource_name_prefix}-lustre"
  })

  timeouts {
    create = "45m"
    update = "45m"
    delete = "45m"
  }
}

# --- Data Repository Association: /models ⇄ s3://<model_store>/models/ ---
#
# S3 is the source of truth. Import events on (NEW / CHANGED / DELETED) reflect
# onboarder writes into Lustre; export events off — workloads never write to
# /models, and the mount is exposed read-only through the PV mountOptions.
# batch_import_meta_data_on_create indexes every pre-existing object at DRA-create
# time (otherwise only files uploaded AFTER DRA creation appear in Lustre).
resource "aws_fsx_data_repository_association" "models" {
  count = var.enable_fsx ? 1 : 0

  file_system_id                   = aws_fsx_lustre_file_system.shared[0].id
  data_repository_path             = "s3://${module.model_store.bucket_name}/${local.model_store_models_prefix}/"
  file_system_path                 = local.fsx_mount_path
  batch_import_meta_data_on_create = true
  imported_file_chunk_size         = 1024
  delete_data_in_filesystem        = false

  s3 {
    auto_import_policy {
      events = ["NEW", "CHANGED", "DELETED"]
    }
    auto_export_policy {
      events = []
    }
  }

  tags = local.combined_tags

  timeouts {
    create = "30m"
    update = "30m"
    delete = "30m"
  }
}

# --- FSx CSI driver: controller IAM (Pod Identity) ---
#
# Least-privilege for the STATIC provisioning shape this template ships (Terraform owns
# the file system, DRA, and PV — the CSI controller only needs to describe the FS at
# attach time; the node plugin needs NO AWS API creds — the SG boundary IS the entire
# access-control story on the data plane). Deliberately NOT the managed FSx-full-access
# policy: that policy includes fsx:DeleteFileSystem / fsx:UpdateFileSystem / DRA writes,
# so a compromise of the CSI driver or a supply-chain hit on its image (chart pulled
# from a floating HTTPS index) could nuke the file system AND hang `jd down` on state
# drift. Adding dynamic provisioning (a StorageClass with `provisioner: fsx.csi.aws.com`)
# would require expanding this policy — do it explicitly then, don't grant it up-front.
data "aws_iam_policy_document" "fsx_csi" {
  count = var.enable_fsx ? 1 : 0

  statement {
    sid    = "DescribeForStaticProvisioning"
    effect = "Allow"
    actions = [
      "fsx:DescribeFileSystems",
      "fsx:DescribeDataRepositoryAssociations",
    ]
    # FSx Describe* actions don't support resource-level permissions per the AWS docs,
    # so scoping to `*` is what the API accepts. Attackers gain read-only Describe on
    # every FSx FS in the account — meaningfully less blast-radius than the managed
    # policy's Delete/Update on the same set.
    resources = ["*"]
  }
}

module "fsx_csi_role" {
  count = var.enable_fsx ? 1 : 0

  source             = "./modules/iam_role"
  role_name          = "${local.resource_name_prefix}-fsx-csi"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
  combined_tags      = local.combined_tags
}

resource "aws_iam_role_policy" "fsx_csi" {
  count  = var.enable_fsx ? 1 : 0
  name   = "${local.resource_name_prefix}-fsx-csi"
  role   = module.fsx_csi_role[0].role_name
  policy = data.aws_iam_policy_document.fsx_csi[0].json
}

# --- FSx CSI driver: Helm release ---
#
# Not published as an EKS managed addon, so installed via Helm from
# kubernetes-sigs.github.io. Controller pinned to the tainted system NG; node
# plugin DaemonSet tolerates all taints so it lands on every node (Karpenter GPU
# nodes included).
#
# Every image the chart references (the FSx CSI driver + 4 CSI sidecars) is on
# public.ecr.aws — a no-creds pull-through upstream in this template — so ALL of
# them MUST be repinned to the private ECR pull-through URI. On the endpoints-only
# VPC nodes can't reach public.ecr.aws directly, and the chart defaults pull from
# there → ErrImagePull → helm timeouts on release create.
resource "helm_release" "fsx_csi_driver" {
  count = var.enable_fsx ? 1 : 0

  name       = "aws-fsx-csi-driver"
  repository = "https://kubernetes-sigs.github.io/aws-fsx-csi-driver"
  chart      = "aws-fsx-csi-driver"
  version    = var.fsx_csi_driver_chart_version
  namespace  = local.fsx_namespace

  set = [
    # Controller pod → tainted system NG.
    { name = "controller.nodeSelector.inference/role", value = "system" },
    { name = "controller.tolerations[0].key", value = "inference/role" },
    { name = "controller.tolerations[0].operator", value = "Equal" },
    { name = "controller.tolerations[0].value", value = "system" },
    { name = "controller.tolerations[0].effect", value = "NoSchedule" },
    # Node plugin DaemonSet must tolerate ALL taints so it can mount FSx on any node.
    { name = "node.tolerateAllTaints", value = "true" },

    # Repin the FSx CSI driver image to the pull-through URI (PRIMARY resolution).
    { name = "image.repository", value = "${local.ecr_registry}/ecr-public/fsx-csi-driver/aws-fsx-csi-driver" },
    # Repin the 4 CSI sidecars to the pull-through URI. Their default tags are pinned
    # by the chart appVersion; we leave them alone (they float with chart_version).
    { name = "sidecars.livenessProbe.image.repository", value = "${local.ecr_registry}/ecr-public/csi-components/livenessprobe" },
    { name = "sidecars.nodeDriverRegistrar.image.repository", value = "${local.ecr_registry}/ecr-public/csi-components/csi-node-driver-registrar" },
    { name = "sidecars.provisioner.image.repository", value = "${local.ecr_registry}/ecr-public/csi-components/csi-provisioner" },
    { name = "sidecars.resizer.image.repository", value = "${local.ecr_registry}/ecr-public/csi-components/csi-resizer" },
  ]

  depends_on = [
    null_resource.cluster_addons,
    null_resource.pullthrough_ready,
    module.node_group,
  ]
}

resource "aws_eks_pod_identity_association" "fsx_csi" {
  count = var.enable_fsx ? 1 : 0

  cluster_name    = module.eks_cluster.cluster_name
  namespace       = local.fsx_namespace
  service_account = "fsx-csi-controller-sa"
  role_arn        = module.fsx_csi_role[0].role_arn
  tags            = local.combined_tags

  depends_on = [aws_eks_addon.pod_identity_agent]
}

# --- AZ discovery for the pinned subnet ---
#
# Exposed via output so a workload chart / user can add a
# topology.kubernetes.io/zone nodeAffinity that pins consumer pods to the FSx AZ.
data "aws_subnet" "fsx" {
  count = var.enable_fsx ? 1 : 0
  id    = local.fsx_subnet_id
}
