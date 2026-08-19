# === Storage day-1 path ===
#
# The template ships storage INFRASTRUCTURE only — the model-store bucket, the batch
# intake/output buckets, the node/pod S3 grants, the two StorageClasses — never any
# weights (those arrive via onboarder). Day-1 offers two weight-serving paths, both
# fed by the model-store bucket:
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
  lifecycle_rule     = null
}

locals {
  # Key-prefix convention inside the model-store bucket (no resources — just documented
  # layout). models/ = weights (written by onboarder); rehost/ = onboarder artifacts.
  # Batch data never lands here — it lives in the dedicated batch buckets below.
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

# --- Dedicated batch-inference buckets (always created, start empty) ---
#
# Batch data changes frequently (requests in, results and metrics out). This behavior
# differs from the write-once model store. Dedicated buckets keep the write grant away
# from the model weights.
# Intake and output are SEPARATE buckets: requests flow into batch_intake, workers
# publish results and run summaries (metrics/) to batch_output. The bucket boundary
# makes each data flow one-directional and lets retention/lifecycle rules differ.
# The shared bucket module removes batch artifacts after 90 days.
module "batch_intake" {
  source = "./modules/s3_bucket"

  bucket_name_prefix = "${local.resource_name_prefix}-batch-in"
  combined_tags      = local.combined_tags
  lifecycle_rule = {
    id                                     = "expire-batch-data"
    expiration_days                        = 90
    noncurrent_version_expiration_days     = 90
    abort_incomplete_multipart_upload_days = 7
  }
}

module "batch_output" {
  source = "./modules/s3_bucket"

  bucket_name_prefix = "${local.resource_name_prefix}-batch-out"
  combined_tags      = local.combined_tags
  lifecycle_rule = {
    id                                     = "expire-batch-data"
    expiration_days                        = 90
    noncurrent_version_expiration_days     = 90
    abort_incomplete_multipart_upload_days = 7
  }
}

locals {
  batch_inference_service_account_name = "batch-inference"
  batch_storage_config_map_name        = "batch-storage"
}

# @secure_recommendation: Use Pod Identity and exact object actions for batch data.
data "aws_iam_policy_document" "batch_s3" {
  statement {
    sid       = "ListBatchIntake"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [module.batch_intake.bucket_arn]
  }

  statement {
    sid       = "ReadBatchIntake"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${module.batch_intake.bucket_arn}/*"]
  }

  statement {
    sid       = "ListBatchOutput"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [module.batch_output.bucket_arn]
  }

  statement {
    sid       = "ReadWriteBatchOutput"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${module.batch_output.bucket_arn}/*"]
  }
}

module "batch_inference_role" {
  source             = "./modules/iam_role"
  role_name          = "${local.resource_name_prefix}-batch-inference"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
  combined_tags      = local.combined_tags
}

resource "aws_iam_role_policy" "batch_s3" {
  name   = "${local.resource_name_prefix}-batch-s3"
  role   = module.batch_inference_role.role_name
  policy = data.aws_iam_policy_document.batch_s3.json
}

# depends_on kubernetes_namespace_v1.workload (not just the implicit namespace-string
# interpolation): the namespace resource carries the admin-access-association + node-group
# guards, so chaining through it keeps the K8s provider authorized and the nodes alive
# until these objects are deleted on `jd down` (the eks-oidc issue #333 destroy-order lesson).
resource "kubernetes_service_account_v1" "batch_inference" {
  metadata {
    name      = local.batch_inference_service_account_name
    namespace = kubernetes_namespace_v1.workload.metadata[0].name
  }

  depends_on = [kubernetes_namespace_v1.workload]
}

resource "kubernetes_config_map_v1" "batch_storage" {
  metadata {
    name      = local.batch_storage_config_map_name
    namespace = kubernetes_namespace_v1.workload.metadata[0].name
  }

  data = {
    AWS_REGION          = data.aws_region.current.id
    AWS_DEFAULT_REGION  = data.aws_region.current.id
    BATCH_INTAKE_BUCKET = module.batch_intake.bucket_name
    BATCH_OUTPUT_BUCKET = module.batch_output.bucket_name
  }

  depends_on = [kubernetes_namespace_v1.workload]
}

resource "aws_eks_pod_identity_association" "batch_inference" {
  cluster_name    = module.eks_cluster.cluster_name
  namespace       = kubernetes_service_account_v1.batch_inference.metadata[0].namespace
  service_account = kubernetes_service_account_v1.batch_inference.metadata[0].name
  role_arn        = module.batch_inference_role.role_arn
  tags            = local.combined_tags

  depends_on = [
    aws_eks_addon.pod_identity_agent,
    aws_iam_role_policy.batch_s3,
  ]
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
# First-party local chart: the EBS gp3 default class (RWO, dynamic), the s3-models
# static PV/PVC (Mountpoint supports STATIC provisioning only), and — when
# var.enable_fsx is true — the FSx for Lustre static PV/PVC. One helm_release so
# the objects install/uninstall atomically and teardown-order cleanly before the CSI
# drivers (via depends_on cluster_addons + helm_release.fsx_csi_driver).
locals {
  # S3-mount mount path inside consumer pods. The chart doesn't own the mountPath
  # (Mountpoint's client is pointed at whatever the pod's volumeMounts.mountPath is),
  # so this is the CONVENTION the platform publishes for tracks to follow. Matches
  # the FSx mount path so a track that flips between backends doesn't have to change
  # its container's volume mount path.
  s3_mount_path = "/models"

  # PVC name + namespace the storage chart materializes for S3-mount. The
  # s3_mount_platform_info ConfigMap advertises these so tracks discover them
  # rather than hardcoding.
  s3_mount_pvc_name      = "model-store"
  s3_mount_pvc_namespace = "default"
}

resource "helm_release" "storage" {
  name      = "storage"
  chart     = "${path.module}/../charts/storage"
  namespace = "kube-system"

  set = concat(
    [
      { name = "ebs.default", value = "true" },
      { name = "s3.bucketName", value = module.model_store.bucket_name },
      { name = "s3.region", value = data.aws_region.current.id },
      { name = "s3.modelsPrefix", value = local.model_store_models_prefix },
      # Chart content hash so editing a chart file triggers a re-apply (see main.tf).
      { name = "chartContentHash", value = local.chart_hashes["storage"] },
      { name = "fsx.enabled", value = tostring(var.enable_fsx) },
    ],
    # FSx values populated only when the file system exists; otherwise fsx.enabled=false
    # short-circuits the chart's fsx-mount.yaml template.
    var.enable_fsx ? [
      { name = "fsx.fileSystemId", value = aws_fsx_lustre_file_system.shared[0].id },
      { name = "fsx.dnsName", value = aws_fsx_lustre_file_system.shared[0].dns_name },
      { name = "fsx.mountName", value = aws_fsx_lustre_file_system.shared[0].mount_name },
      # AZ hint embedded on the PV as spec.nodeAffinity — see charts/storage/templates/fsx-mount.yaml.
      { name = "fsx.availabilityZone", value = data.aws_subnet.fsx[0].availability_zone },
      { name = "fsx.capacity", value = "${var.fsx_storage_capacity_gib}Gi" },
      { name = "fsx.claimNamespace", value = kubernetes_namespace_v1.workload.metadata[0].name },
    ] : [],
  )

  depends_on = [
    null_resource.cluster_addons,
    aws_eks_addon.s3_csi_driver,
    module.node_group,
    helm_release.fsx_csi_driver,
    aws_fsx_data_repository_association.models,
  ]
}

# --- s3-mount-platform-info ConfigMap: peer discovery handle for S3-mount ---
#
# Symmetric with fsx-platform-info (platform_fsx.tf). Publishes S3-mount identity as
# a first-class K8s object so tracks discover storage backends by listing
# `-l platform.inference/kind=storage` — one code path across FSx and S3-mount rather
# than "check-for-ConfigMap else hardcode-defaults." Unconditional (S3-mount is
# always on, unlike FSx which is opt-in).
#
# `capabilities` and `platformPvcNamespace` are the load-bearing fields for
# backend selection:
#   - capabilities: "read-only, partial-posix" — surfaces the honest constraints
#     (Mountpoint doesn't support atomic renames, POSIX locking, or writes to
#     existing keys). A track that needs RWX/POSIX rejects this backend on read.
#   - platformPvcNamespace: today the S3-mount PVC lands in "default" (values.yaml
#     default, never overridden). Cross-namespace PVC consumption isn't supported
#     natively, so the ConfigMap surfaces the ACTUAL location. A follow-up will
#     relocate it to the workload namespace (matching the FSx placement); the
#     ConfigMap update rolls with that move.
resource "kubernetes_config_map_v1" "s3_mount_platform_info" {
  metadata {
    name      = "s3-mount-platform-info"
    namespace = kubernetes_namespace_v1.workload.metadata[0].name
    labels = {
      "platform.inference/kind"    = "storage"
      "platform.inference/backend" = "s3-mount"
    }
    annotations = {
      "platform.inference/deployment-id" = random_id.postfix.hex
    }
  }

  data = {
    bucketName             = module.model_store.bucket_name
    region                 = data.aws_region.current.id
    modelsPrefix           = local.model_store_models_prefix
    mountPath              = local.s3_mount_path
    dataRepositoryPath     = "s3://${module.model_store.bucket_name}/${local.model_store_models_prefix}/"
    platformPvcName        = local.s3_mount_pvc_name
    platformPvcNamespace   = local.s3_mount_pvc_namespace
    # Honest labeling: Mountpoint mounts are ReadOnlyMany + partial POSIX. Tracks
    # that need RWX or full POSIX (locking, atomic rename) reject this backend on
    # read and fall through to FSx (or fail loud when FSx isn't enabled).
    capabilities           = "read-only, partial-posix"
  }

  depends_on = [
    null_resource.cluster_addons,
    module.node_group,
    helm_release.storage,
  ]
}
