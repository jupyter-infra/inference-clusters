# === Storage day-1 path ===
#
# The template ships storage INFRASTRUCTURE only — the model-store bucket, the batch
# bucket, the node/pod S3 grants, the two StorageClasses — never any weights (those
# arrive via onboarder). Day-1 offers two weight-serving paths, both fed by the
# model-store bucket:
#   1. S3-direct: the engine streams weights straight from S3 (vLLM RunAI streamer /
#      Tensorizer / SDK) using the NODE ROLE's S3 grant — no filesystem.
#   2. S3-mount: the Mountpoint-for-S3 CSI driver mounts s3://<bucket>/models as a
#      read-only POSIX path via the s3-models StorageClass (static PV), using a
#      dedicated Pod Identity role.

# --- Shared model bucket (always created, starts empty) ---
module "model_store" {
  source = "./modules/s3_bucket"

  # random_id keeps two deployments in one account/region conflict-free (project rule).
  bucket_name_prefix = "${local.resource_name_prefix}-store"
  combined_tags      = local.combined_tags
}

locals {
  # Key-prefix convention inside the model-store bucket (no resources — just documented
  # layout). models/ = weights (written by onboarder); rehost/ = onboarder artifacts.
  # Batch data never lands here — it lives in the dedicated batch_store bucket below.
  model_store_models_prefix = "models"
}

# --- S3-direct path: node-role grant (model store, read-only) ---
#
# containerd/kubelet and any pod on any node reach the bucket through the node
# instance role — no per-chart wiring. Scoped to THIS bucket ARN (never *) and
# READ-ONLY: only the onboarder writes weights, so workloads cannot alter them.
# This is the day-1 streaming grant AND what a pod's AWS SDK uses for S3-direct
# weight loading.
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
}

resource "aws_iam_role_policy" "node_s3" {
  name   = "${local.resource_name_prefix}-node-s3"
  role   = module.node_role.role_name
  policy = data.aws_iam_policy_document.node_s3.json
}

# --- Dedicated batch-inference bucket (always created, starts empty) ---
#
# Batch data is high-churn (requests in, results + metrics out, benchmark cleanup
# deletes) — the opposite lifecycle of the write-once model store. A dedicated bucket
# keeps that churn, and the write grant it needs, away from the weights entirely.
module "batch_store" {
  source = "./modules/s3_bucket"

  bucket_name_prefix = "${local.resource_name_prefix}-batch"
  combined_tags      = local.combined_tags
}

locals {
  # Key-prefix conventions inside the batch bucket (documented layout, not resources):
  # requests land under intake/; workers publish results under output/ and run
  # summaries under metrics/.
  batch_store_intake_prefix  = "intake"
  batch_store_output_prefix  = "output"
  batch_store_metrics_prefix = "metrics"
}

# Bespoke node-role grant for the batch bucket: full object lifecycle (get, put,
# delete) but ONLY under the three batch prefixes — never the bucket root, never *.
# AbortMultipartUpload is the cleanup path SDK uploads use when a large multipart
# result upload fails partway (same rationale as the onboarder grant).
data "aws_iam_policy_document" "node_batch_s3" {
  statement {
    sid       = "ListBatchStore"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [module.batch_store.bucket_arn]
  }
  statement {
    sid     = "ManageBatchData"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
    resources = [
      "${module.batch_store.bucket_arn}/${local.batch_store_intake_prefix}/*",
      "${module.batch_store.bucket_arn}/${local.batch_store_output_prefix}/*",
      "${module.batch_store.bucket_arn}/${local.batch_store_metrics_prefix}/*",
    ]
  }
}

resource "aws_iam_role_policy" "node_batch_s3" {
  name   = "${local.resource_name_prefix}-node-batch-s3"
  role   = module.node_role.role_name
  policy = data.aws_iam_policy_document.node_batch_s3.json
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
