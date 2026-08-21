# === FSx for Lustre — opt-in RWX weight cache ===
#
# Third weight-serving path alongside S3-direct and Mountpoint-for-S3, for RWX POSIX
# workloads (LoRA/checkpoint scratch, KV-cache offload, shared dataset caches). Off
# by default (non-trivial hourly cost, single-AZ). Provisions PERSISTENT_2 SSD + LZ4
# + DRA to s3://<model_store>/models/ (auto-import on, auto-export off — S3 stays
# source of truth), a dedicated SG for Lustre ports (988, 1018-1023), and the
# aws-fsx-csi-driver Helm release with a least-privilege Describe-only IAM role.
# FSx lands in private_subnet_ids[0]; consumer pods should pin to
# output.fsx_availability_zone via topology.kubernetes.io/zone nodeAffinity.

locals {
  fsx_namespace  = "kube-system"
  fsx_mount_path = "/models"
  fsx_subnet_id  = var.enable_fsx ? module.vpc.private_subnet_ids[0] : ""

  # Total GPU capacity the cluster's Karpenter NodePools may provision. Kueue caps
  # nominalQuota to these same values, so this is the effective ceiling on
  # concurrent readers hitting FSx. P-pool contribution is zero when disabled
  # (the NodePool CR ships but no pod can request it — see charts/karpenter/templates).
  fsx_total_gpu_capacity = var.gpu_g_capacity + (var.enable_gpu_p_nodepool ? var.gpu_p_capacity : 0)

  # Sentinel 0 → auto-derive throughput. Rough heuristic: bump tier up as
  # concurrent-reader count rises. Cold-scale-out (many pods starting at once)
  # is the real saturation risk — Lustre's per-node page cache absorbs
  # steady-state read pressure but not the first-touch fan-out.
  #   ≤ 20 GPUs           : 250 MB/s/TiB (~$700/mo at 4800 GiB, ~1.17 GB/s aggregate)
  #   20 < N ≤ 60         : 500 MB/s/TiB (~$1,400/mo, ~2.34 GB/s)
  #   > 60 GPUs (P-heavy) : 1000 MB/s/TiB (~$2,800/mo, ~4.68 GB/s)
  # Non-P clusters stay at 250 regardless of g-tier count — g-tier NICs (25 Gbps
  # on g5) can't drive the higher tiers into saturation for realistic workloads.
  # Any explicit non-zero var value pins.
  fsx_derived_per_unit_throughput = (
    !var.enable_gpu_p_nodepool ? 250 :
    local.fsx_total_gpu_capacity > 60 ? 1000 :
    local.fsx_total_gpu_capacity > 20 ? 500 :
    250
  )
  fsx_per_unit_storage_throughput = (
    var.fsx_per_unit_storage_throughput != 0
    ? var.fsx_per_unit_storage_throughput
    : local.fsx_derived_per_unit_throughput
  )

  # FS-wide throughput ceiling in bytes/second — the saturation alarms in
  # platform_fsx_observability.tf compare 5-min-window read/write byte sums
  # against this ceiling × 300s to compute a saturation ratio.
  # Formula: (capacity_gib × per_unit_throughput_MBps_per_TiB / 1024_GiB_per_TiB)
  #          × 1024^2 B/MB = capacity_gib × per_unit_throughput × 1024
  fsx_aggregate_bytes_per_sec       = var.fsx_storage_capacity_gib * local.fsx_per_unit_storage_throughput * 1024
  fsx_throughput_5min_ceiling_bytes = local.fsx_aggregate_bytes_per_sec * 300
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
  per_unit_storage_throughput = local.fsx_per_unit_storage_throughput
  data_compression_type       = "LZ4"
  file_system_type_version    = "2.15"
  kms_key_id                  = var.fsx_kms_key_arn == "" ? null : var.fsx_kms_key_arn

  subnet_ids         = [local.fsx_subnet_id]
  security_group_ids = [aws_security_group.fsx[0].id]

  # 2:09:00 UTC = Tuesday 09:00 UTC — quietest global window for a US-focused fleet
  # (avoids Sat evening PT / prime EU work hours). Any brief maintenance IO blip
  # lands mid-workday for the ops team, when someone can eyeball it, not weekend.
  weekly_maintenance_start_time   = "2:09:00"
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

# --- Data Repository Association: Lustre root ⇄ s3://<model_store>/models/ ---
#
# `file_system_path = "/"` maps the S3 `models/` prefix directly to the Lustre
# root. The FSx CSI PV mounts Lustre root (no subdir) at the pod's mountpoint,
# and pods mount at /models (see local.fsx_mount_path + charts/storage/fsx-mount.yaml),
# so an S3 object at `models/model-a/config.json` shows up at pod path
# `/models/model-a/config.json` — same layout as the S3-mount (Mountpoint) PV,
# so a track can flip backends without changing its mount paths.
#
# Earlier attempts set `file_system_path = "/models"` here. That put S3 content
# at Lustre `/models/*`, which the PV (rooted at Lustre `/`) then exposed at pod
# `/models/models/*` — one level deeper than every consumer expected. The
# hydration Job's `/mnt/models/$PREFIX` path then referenced Lustre `/$PREFIX`
# (a nonexistent path), took the "doesn't exist" fallback branch, touched the
# sentinel, and reported success while warming zero bytes. Roborev flagged this
# on every commit before the fix.
#
# S3 is the source of truth. Import events reflect onboarder writes into Lustre;
# export events off — workloads never write back.
# batch_import_meta_data_on_create indexes every pre-existing object at DRA-create
# time (otherwise only files uploaded AFTER DRA creation appear in Lustre).
#
# DELETED intentionally NOT in auto_import events: an S3-side delete (lifecycle
# rule fire, compromised principal, mis-configured bucket policy) would otherwise
# propagate to Lustre within seconds and evict the running workload's weights
# with no undo path. Explicit resync via `terraform destroy` on the DRA + reapply
# stays the only path — an auditable operator action, not a runbook near-miss.
#
# imported_file_chunk_size is the S3-object → Lustre-OST stripe granularity at
# metadata-import time. AWS recommends 16 MiB for large tensor files so a single
# weight file fans across OSTs (parallel reads); 1024 MiB (the old default) makes
# every file < 1 GiB single-server and caps read throughput at one OSS's tier.
resource "aws_fsx_data_repository_association" "models" {
  count = var.enable_fsx ? 1 : 0

  file_system_id                   = aws_fsx_lustre_file_system.shared[0].id
  data_repository_path             = "s3://${module.model_store.bucket_name}/${local.model_store_models_prefix}/"
  file_system_path                 = "/"
  batch_import_meta_data_on_create = true
  imported_file_chunk_size         = var.fsx_imported_file_chunk_size_mib
  delete_data_in_filesystem        = false

  s3 {
    auto_import_policy {
      events = ["NEW", "CHANGED"]
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

# --- fsx-platform-info ConfigMap: first-class K8s handle to platform FSx state ---
#
# Publishes FSx identity + sizing as a discoverable in-cluster ConfigMap in the
# workload namespace. Purpose: give consumers (KRO blocks, workload initContainers,
# humans with kubectl) a declarative K8s API for platform storage state instead
# of routing every value through `jupyter-deploy show --output NAME` + a Python
# substitution engine on the deployer's host.
#
# Consumers wire it in whichever native K8s way fits — envFrom on a pod, a KRO
# graph resource that reads it, or plain `kubectl get cm -o jsonpath` in a script.
# The label `platform.inference/kind: storage` groups it with the peer
# s3-mount-platform-info ConfigMap (see platform_storage.tf) so tracks list one
# label to see every available storage backend rather than hardcode names or
# probe for existence of specific ConfigMaps.
#
# All values are string-typed because ConfigMap.data is stringMap. Consumers that
# need numbers parse them (or use envFrom + shell arithmetic). `aggregateGBpsMax`
# is pre-computed here so consumers don't re-derive the formula.
resource "kubernetes_config_map_v1" "fsx_platform_info" {
  count = var.enable_fsx ? 1 : 0

  metadata {
    name      = "fsx-platform-info"
    namespace = kubernetes_namespace_v1.workload.metadata[0].name
    labels = {
      "platform.inference/kind"    = "storage"
      "platform.inference/backend" = "fsx-lustre"
    }
    annotations = {
      # Consumers can gate on this to detect config changes across `jd up` runs.
      "platform.inference/deployment-id" = random_id.postfix.hex
    }
  }

  data = {
    fileSystemId       = aws_fsx_lustre_file_system.shared[0].id
    dnsName            = aws_fsx_lustre_file_system.shared[0].dns_name
    mountName          = aws_fsx_lustre_file_system.shared[0].mount_name
    availabilityZone   = data.aws_subnet.fsx[0].availability_zone
    dataRepositoryPath = aws_fsx_data_repository_association.models[0].data_repository_path
    mountPath          = local.fsx_mount_path

    # Sizing knobs — string-typed for ConfigMap. Consumers that want numbers parse.
    storageCapacityGib          = tostring(var.fsx_storage_capacity_gib)
    perUnitThroughputMBpsPerTiB = tostring(local.fsx_per_unit_storage_throughput)

    # Pre-computed aggregate ceiling in GB/s so consumers don't repeat the math.
    # capacity_gib × per_unit_MBps / 1024 = aggregate MB/s → /1024 = GB/s. Round
    # down to whole GB/s — a 4800 GiB × 500 MB/s/TiB FS = 2 GB/s (not 2.34).
    aggregateGBpsMax = tostring(floor(var.fsx_storage_capacity_gib * local.fsx_per_unit_storage_throughput / 1024 / 1024))

    # PV/PVC the platform-owned storage chart ships in this namespace — consumers
    # in-namespace can reference this claim directly rather than duplicating a PV
    # via the block. Cross-namespace consumers still need the block (which will
    # itself consume this ConfigMap in a follow-up).
    platformPvcName = "model-store-fsx"
  }

  # Destroy-ordering guardrails — same as every other kubernetes_* resource in
  # this repo. The ConfigMap MUST outlive... nothing much, actually, but the
  # invariant tests want the chain there.
  depends_on = [
    null_resource.cluster_addons,
    module.node_group,
    aws_fsx_data_repository_association.models,
  ]
}
