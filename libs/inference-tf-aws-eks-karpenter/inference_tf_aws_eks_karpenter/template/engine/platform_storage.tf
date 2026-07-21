# === Storage day-1 path ===
#
# The template ships storage INFRASTRUCTURE only — the shared bucket, the node/pod S3
# grant, the two StorageClasses — never any weights (those arrive via onboarder).
# Day-1 offers two weight-serving paths, both fed by the same bucket:
#   1. S3-direct: the engine streams weights straight from S3 (vLLM RunAI streamer /
#      Tensorizer / SDK) using the NODE ROLE's S3 grant — no filesystem.
#   2. S3-mount: the Mountpoint-for-S3 CSI driver mounts s3://<bucket>/models as a
#      read-only POSIX path via the s3-models StorageClass (static PV), using a
#      dedicated Pod Identity role.

# --- Shared model/data bucket (always created, starts empty) ---
module "model_store" {
  source = "./modules/s3_bucket"

  # random_id keeps two deployments in one account/region conflict-free (project rule).
  bucket_name_prefix = "${local.resource_name_prefix}-store"
  combined_tags      = local.combined_tags
}

locals {
  # Key-prefix conventions inside the one bucket (no resources — just documented
  # layout). models/ = weights (written by onboarder); intake/+output/ = batch.
  model_store_models_prefix  = "models"
  model_store_intake_prefix  = "intake"
  model_store_output_prefix  = "output"
  model_store_metrics_prefix = "metrics"
}

# --- S3-direct path: node-role grant ---
#
# containerd/kubelet and any pod on any node reach the bucket through the node
# instance role — no per-chart wiring. Scoped to THIS bucket ARN (never *): read
# anywhere in the bucket. The role can write and delete only batch-data objects.
# This is the day-1 streaming grant and the credential path for worker SDKs.
data "aws_iam_policy_document" "node_s3" {
  statement {
    sid       = "ListModelStore"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [module.model_store.bucket_arn]
  }
  statement {
    sid       = "ReadModelStore"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${module.model_store.bucket_arn}/*"]
  }
  statement {
    sid     = "WriteBatchData"
    effect  = "Allow"
    actions = ["s3:PutObject", "s3:DeleteObject"]
    resources = [
      "${module.model_store.bucket_arn}/${local.model_store_intake_prefix}/*",
      "${module.model_store.bucket_arn}/${local.model_store_output_prefix}/*",
      "${module.model_store.bucket_arn}/${local.model_store_metrics_prefix}/*",
    ]
  }
}

resource "aws_iam_role_policy" "node_s3" {
  name   = "${local.resource_name_prefix}-node-s3"
  role   = module.node_role.role_name
  policy = data.aws_iam_policy_document.node_s3.json
}

# --- S3-mount path: dedicated Pod Identity role for the Mountpoint CSI driver ---
#
# The Mountpoint-for-S3 CSI driver authenticates with THIS role (Pod Identity on its
# controller SA), not the node role — least-privilege, decoupled from the broad node
# grant. Read-only on the models/ prefix (the mount is read-only).
data "aws_iam_policy_document" "s3_csi" {
  statement {
    sid       = "MountpointListModels"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [module.model_store.bucket_arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${local.model_store_models_prefix}/*", local.model_store_models_prefix]
    }
  }
  statement {
    sid       = "MountpointReadModels"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${module.model_store.bucket_arn}/${local.model_store_models_prefix}/*"]
  }
}

module "s3_csi_role" {
  source             = "./modules/iam_role"
  role_name          = "${local.resource_name_prefix}-s3-csi"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
  combined_tags      = local.combined_tags
}

resource "aws_iam_role_policy" "s3_csi" {
  name   = "${local.resource_name_prefix}-s3-csi"
  role   = module.s3_csi_role.role_name
  policy = data.aws_iam_policy_document.s3_csi.json
}

# --- StorageClasses + S3 mount PV/PVC (charts/storage) ---
#
# First-party local chart: the EBS gp3 default class (RWO, dynamic) + the s3-models
# static PV/PVC (Mountpoint supports STATIC provisioning only). One helm_release so
# the objects install/uninstall atomically and teardown-order cleanly before the CSI
# drivers (via depends_on cluster_addons). FSx RWX class is added here later.
resource "helm_release" "storage" {
  name      = "storage"
  chart     = "${path.module}/../charts/storage"
  namespace = "kube-system"

  set = [
    { name = "ebs.default", value = "true" },
    { name = "s3.bucketName", value = module.model_store.bucket_name },
    { name = "s3.region", value = data.aws_region.current.id },
    { name = "s3.modelsPrefix", value = local.model_store_models_prefix },
    # Chart content hash so editing a chart file triggers a re-apply (see main.tf).
    { name = "chartContentHash", value = local.chart_hashes["storage"] },
  ]

  depends_on = [
    null_resource.cluster_addons,
    aws_eks_addon.s3_csi_driver,
    module.node_group,
  ]
}
