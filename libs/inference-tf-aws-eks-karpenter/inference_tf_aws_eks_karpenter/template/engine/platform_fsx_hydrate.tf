# === FSx for Lustre — S3 → Lustre hydration ===
#
# The DRA imports S3 object METADATA (namespace) at creation, but file BYTES are
# lazy-loaded on first read — the first pod pays a per-file S3-download tax at
# cold start. This file ships one Kubernetes Job per var.fsx_hydrate_prefixes
# entry that eagerly warms those bytes with `lfs hsm_restore` and stripes them
# across all OSTs with `lfs setstripe -c -1 -S 4M`, then writes a
# `.hydrated-<slug>` sentinel workload initContainers can gate on.
#
# Gated on enable_fsx AND non-empty fsx_hydrate_prefixes — an FSx-enabled
# cluster with no prefixes is still valid (matches the "you can enable FSx
# without pre-warming" opt-in shape).
#
# Not the onboarder: onboarder is CodeBuild with no cluster access.
# Not the workload chart/track: per-track hydration over-fetches when N tracks
# share a model store, duplicates work, and puts hydration lifecycle in track
# authors' hands. Platform-side is the only place with the whole picture.

locals {
  fsx_hydrate_enabled  = var.enable_fsx && length(var.fsx_hydrate_prefixes) > 0
  fsx_hydrate_prefixes = local.fsx_hydrate_enabled ? var.fsx_hydrate_prefixes : []

  # Per-prefix identifier used for the Job name, sentinel filename, and DRT report
  # sub-path. Prefix may contain slashes ("adapters/lora-v1"); flatten to a
  # DNS-1123-compatible slug for the K8s resource name.
  fsx_hydrate_prefix_slugs = {
    for p in local.fsx_hydrate_prefixes :
    p => replace(replace(p, "/", "-"), "_", "-")
  }

  # S3 URI the DRT completion report is written to (per-prefix subpath under the
  # existing model_store bucket). PutObject scope in the hydrator IAM points here.
  fsx_hydrate_report_prefix = "fsx-drt-reports"
  fsx_hydrate_report_uri    = "s3://${module.model_store.bucket_name}/${local.fsx_hydrate_report_prefix}"
}

# --- Hydrator IAM: Pod Identity, tightly scoped ---
#
# fsx:CreateDataRepositoryTask/Describe/Cancel scoped to this file system's ARN
# via aws:ResourceTag/DeploymentId — matches the pattern the Karpenter controller
# role uses to scope EC2 actions per-cluster. Refuses cross-deployment DRT ops
# even if two clusters coexist in one account.
data "aws_iam_policy_document" "fsx_hydrator" {
  count = local.fsx_hydrate_enabled ? 1 : 0

  statement {
    sid    = "DrtOnOurFileSystem"
    effect = "Allow"
    actions = [
      "fsx:CreateDataRepositoryTask",
      "fsx:DescribeDataRepositoryTasks",
      "fsx:CancelDataRepositoryTask",
      "fsx:TagResource",
    ]
    resources = [
      aws_fsx_lustre_file_system.shared[0].arn,
      # DRT ARNs are of the form arn:aws:fsx:<region>:<account>:task/task-*.
      # DescribeDataRepositoryTasks needs read on the task resource itself.
      "arn:${data.aws_partition.current.partition}:fsx:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:task/*",
    ]
  }

  statement {
    sid    = "PutDrtReports"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:PutObjectAcl",
    ]
    resources = ["${module.model_store.bucket_arn}/${local.fsx_hydrate_report_prefix}/*"]
  }

  statement {
    sid       = "DrtReportBucketLocation"
    effect    = "Allow"
    actions   = ["s3:GetBucketLocation"]
    resources = [module.model_store.bucket_arn]
  }
}

module "fsx_hydrator_role" {
  count = local.fsx_hydrate_enabled ? 1 : 0

  source             = "./modules/iam_role"
  role_name          = "${local.resource_name_prefix}-fsx-hydrator"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
  combined_tags      = local.combined_tags
}

resource "aws_iam_role_policy" "fsx_hydrator" {
  count  = local.fsx_hydrate_enabled ? 1 : 0
  name   = "${local.resource_name_prefix}-fsx-hydrator"
  role   = module.fsx_hydrator_role[0].role_name
  policy = data.aws_iam_policy_document.fsx_hydrator[0].json
}

resource "kubernetes_service_account_v1" "fsx_hydrator" {
  count = local.fsx_hydrate_enabled ? 1 : 0

  metadata {
    name      = "fsx-hydrator"
    namespace = kubernetes_namespace_v1.workload.metadata[0].name
  }

  # Same destroy-ordering guardrails every K8s resource in this repo carries:
  # admin access-policy associations MUST outlive the SA (K8s provider auth), and
  # the node group MUST outlive it (in case a controller reconciles pods during
  # destroy — see the eks-oidc lesson referenced in CLAUDE.md).
  depends_on = [
    null_resource.cluster_addons,
    module.node_group,
  ]
}

resource "aws_eks_pod_identity_association" "fsx_hydrator" {
  count = local.fsx_hydrate_enabled ? 1 : 0

  cluster_name    = module.eks_cluster.cluster_name
  namespace       = kubernetes_service_account_v1.fsx_hydrator[0].metadata[0].namespace
  service_account = kubernetes_service_account_v1.fsx_hydrator[0].metadata[0].name
  role_arn        = module.fsx_hydrator_role[0].role_arn
  tags            = local.combined_tags

  depends_on = [aws_eks_addon.pod_identity_agent]
}

# --- Hydration Job — one per prefix ---
#
# Uses amazonlinux:2023 via ECR pull-through: AL2023 dnf ships lustre2.15-client
# and aws-cli, so we install both at container start rather than vendor another
# image via CodeBuild. Adds ~30s to Job cold-start; acceptable for a one-shot.
#
# The Job:
#   1. Verifies /mnt/models is a lustre mount (bails early if the CSI handed us
#      the wrong FS somehow).
#   2. Fires an FSx DRT of type IMPORT_METADATA_FROM_REPOSITORY to refresh the
#      Lustre namespace against S3 (idempotent; picks up any post-DRA-create
#      onboarder writes).
#   3. `lfs setstripe -c -1 -S 4M` on the prefix dir so future files stride
#      across all OSTs.
#   4. `find | xargs -P 32 lfs hsm_restore` to pre-fetch every file's bytes.
#   5. Polls `lfs hsm_state` until no file is "released" (all warm).
#   6. Touches `.hydrated-<slug>` sentinel that workload initContainers gate on.
#
# TTL 24h so a failed hydration is preserved for postmortem, then reaped.
# activeDeadlineSeconds 2h caps a stuck restore (S3 rate-limited, misconfigured
# bucket policy, etc.) so we don't burn a stuck Job indefinitely.
resource "kubernetes_job_v1" "fsx_hydrate" {
  for_each = local.fsx_hydrate_prefix_slugs

  metadata {
    name      = "fsx-hydrate-${each.value}"
    namespace = kubernetes_namespace_v1.workload.metadata[0].name
    labels = {
      "app.kubernetes.io/name"      = "fsx-hydrate"
      "app.kubernetes.io/component" = "hydration"
      "inference/fsx-prefix"        = each.value
    }
    annotations = {
      # Track which enable_fsx=true apply owned this Job — helps postmortem when
      # multiple hydration attempts stack up.
      "inference/deployment-id" = random_id.postfix.hex
    }
  }

  spec {
    backoff_limit              = 3
    active_deadline_seconds    = 7200  # 2h — leaves headroom for a full model refetch
    ttl_seconds_after_finished = 86400 # 24h — keep for postmortem, then reap
    completions                = 1
    parallelism                = 1

    template {
      metadata {
        labels = {
          "app.kubernetes.io/name"      = "fsx-hydrate"
          "app.kubernetes.io/component" = "hydration"
          "inference/fsx-prefix"        = each.value
        }
      }

      spec {
        service_account_name = kubernetes_service_account_v1.fsx_hydrator[0].metadata[0].name
        restart_policy       = "OnFailure"

        # AZ-pinned to the FSx AZ. Cross-AZ metadata polling for the hsm_state
        # loop is a real per-op latency tax we don't need to eat on hydration.
        affinity {
          node_affinity {
            required_during_scheduling_ignored_during_execution {
              node_selector_term {
                match_expressions {
                  key      = "topology.kubernetes.io/zone"
                  operator = "In"
                  values   = [data.aws_subnet.fsx[0].availability_zone]
                }
              }
            }
          }
        }
        # Tolerate the system MNG taint so a Job can land on the always-on
        # system node if one is in the FSx AZ; otherwise Karpenter provisions a
        # small CPU node in that AZ.
        toleration {
          key      = "inference/role"
          operator = "Equal"
          value    = "system"
          effect   = "NoSchedule"
        }

        volume {
          name = "models"
          persistent_volume_claim {
            claim_name = "model-store-fsx"
          }
        }

        container {
          name  = "hydrator"
          image = "${local.ecr_registry}/ecr-public/amazonlinux/amazonlinux:2023"

          env {
            name  = "FS_ID"
            value = aws_fsx_lustre_file_system.shared[0].id
          }
          env {
            name  = "PREFIX"
            value = each.key
          }
          env {
            name  = "SLUG"
            value = each.value
          }
          env {
            name  = "REPORT_URI"
            value = local.fsx_hydrate_report_uri
          }
          env {
            name  = "AWS_REGION"
            value = data.aws_region.current.id
          }

          command = ["/bin/bash", "-c"]
          args = [
            <<-BASH
              set -euo pipefail

              echo "[hydrate] install lustre-client + aws-cli"
              dnf install -y --quiet lustre2.15-client aws-cli findutils >/dev/null

              echo "[hydrate] verify /mnt/models is lustre"
              mount | awk '$3 == "/mnt/models" && $5 == "lustre" { found=1 } END { exit !found }' \
                || { echo "[hydrate] FAIL: /mnt/models is not lustre"; mount; exit 1; }

              # 1) Refresh Lustre namespace against S3 via a DRT (async on AWS side).
              # DRT --paths is a Lustre-absolute path. The DRA is file_system_path="/"
              # (Lustre root ⇄ s3://<bucket>/models/), so a workload prefix like
              # "model-a" maps to Lustre "/model-a" — NOT "/models/model-a". The pod
              # mounts Lustre root at /mnt/models, so /mnt/models/$PREFIX below is
              # the SAME Lustre location the DRT is targeting here.
              echo "[hydrate] create DRT IMPORT_METADATA_FROM_REPOSITORY for /$PREFIX"
              TASK_ID=$(aws fsx create-data-repository-task \
                --file-system-id "$FS_ID" \
                --type IMPORT_METADATA_FROM_REPOSITORY \
                --paths "/$PREFIX" \
                --report "Enabled=true,Path=$REPORT_URI/$SLUG,Format=REPORT_CSV_20191124,Scope=FAILED_FILES_ONLY" \
                --region "$AWS_REGION" \
                --query 'DataRepositoryTask.TaskId' --output text)
              echo "[hydrate] DRT task-id: $TASK_ID"

              # Poll to SUCCEEDED / FAILED.
              while :; do
                STATE=$(aws fsx describe-data-repository-tasks \
                  --task-ids "$TASK_ID" --region "$AWS_REGION" \
                  --query 'DataRepositoryTasks[0].Lifecycle' --output text)
                case "$STATE" in
                  SUCCEEDED) echo "[hydrate] DRT succeeded"; break ;;
                  FAILED|CANCELED) echo "[hydrate] DRT terminal state: $STATE"; exit 1 ;;
                  *) sleep 15 ;;
                esac
              done

              # 2) Stripe across all OSTs so future files land parallelizable.
              #    -c -1 = all OSTs; -S 4M = 4 MiB stripe granularity.
              if [ -d "/mnt/models/$PREFIX" ]; then
                echo "[hydrate] lfs setstripe /mnt/models/$PREFIX"
                lfs setstripe -c -1 -S 4M "/mnt/models/$PREFIX" || echo "[hydrate] setstripe warning (non-fatal)"
              else
                echo "[hydrate] WARN: /mnt/models/$PREFIX doesn't exist after DRT — no bytes to warm"
                touch "/mnt/models/.hydrated-$SLUG"
                exit 0
              fi

              # 3) Pre-fetch every file's bytes (async on Lustre side).
              echo "[hydrate] hsm_restore fan-out (P=32)"
              find "/mnt/models/$PREFIX" -type f -print0 \
                | xargs -0 -r -P 32 -n 32 lfs hsm_restore 2>/dev/null || true

              # 4) Poll until every file is EXISTS (not released). Bounds by
              #    activeDeadlineSeconds at the K8s layer; this inner loop is a
              #    generous fallback ceiling that also emits progress.
              echo "[hydrate] awaiting hsm_state != released"
              DEADLINE=$(( $(date +%s) + 5400 ))  # 90 min soft cap
              while :; do
                PENDING=$(find "/mnt/models/$PREFIX" -type f -print0 \
                  | xargs -0 -r -n 64 lfs hsm_state 2>/dev/null \
                  | grep -c "released" || true)
                if [ "$PENDING" -eq 0 ]; then break; fi
                echo "[hydrate] $PENDING files still released — waiting"
                [ "$(date +%s)" -ge "$DEADLINE" ] && \
                  { echo "[hydrate] soft timeout — $PENDING files still released"; exit 1; }
                sleep 30
              done

              # 5) Sentinel — workload initContainers gate on this file existing.
              touch "/mnt/models/.hydrated-$SLUG"
              echo "[hydrate] OK — .hydrated-$SLUG"
            BASH
            ,
          ]

          volume_mount {
            name       = "models"
            mount_path = "/mnt/models"
          }

          resources {
            requests = {
              cpu    = "500m"
              memory = "512Mi"
            }
            # No CPU limit — lfs is subject to LNet timeouts under CPU throttling.
            limits = {
              memory = "2Gi"
            }
          }
        }
      }
    }
  }

  # Wait for pod completion — a K8s Job is "done" only when its pod Succeeds.
  # Terraform blocks the apply until the hydration finishes, so an operator
  # sees a green `jd up` iff every prefix is warm. Attribute sits at the resource
  # top level (NOT inside spec) — the v3.x provider rejects it inside spec.
  wait_for_completion = true

  timeouts {
    create = "2h30m"
    update = "2h30m"
  }

  depends_on = [
    # Storage chart owns the model-store-fsx PVC we mount.
    helm_release.storage,
    # DRA must be AVAILABLE — DRT calls fail against a still-CREATING DRA.
    aws_fsx_data_repository_association.models,
    # Pod Identity must be bound before the Job pod tries to use its SA.
    aws_eks_pod_identity_association.fsx_hydrator,
    # Namespace + SA must exist.
    kubernetes_service_account_v1.fsx_hydrator,
  ]
}
