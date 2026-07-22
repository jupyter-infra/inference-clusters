# === Chart-onboard job — the consumer workload-consumption contract ===
#
# A CodeBuild job a consumer invokes AFTER the cluster is up (NOT a `jd up` step) to
# rehost a whole inference chart's artifacts onto the endpoints-only VPC and hand back
# ONE overrides.yaml. It:
#   - digest-vendors the chart's mandated `images:` block to <ecr>/workload/<repo>@<digest>
#   - (optional) s5cmd-ingests the chart's `weights:` source into s3://<bucket>/models/<name>
#   - emits overrides.yaml to s3://<bucket>/rehost/out/<chart>/
#   - backstop: `helm template` with the overrides, asserting every image resolves to
#     our ECR (a convention violation fails the build, not a hung pod on the no-NAT VPC)
#
# It handles TWO auto-detected input formats: a Helm chart (Chart.yaml -> emits
# overrides.yaml) or a KRO graph (graph.yaml + sidecar values.yaml of field-paths ->
# emits graph-air-gapped.yaml). See engine/onboarder.py for the contract.
#
# It is a SECOND instance of modules/codebuild_job (distinct from the platform
# image_vendor job in images.tf): different buildspec, larger compute (weights are
# 10s-100s of GB), and different IAM (workload/* ECR + the shared S3 bucket). The
# job's logic lives in engine/onboarder.py, inlined into the buildspec so the same
# module is unit-testable off-CodeBuild against mock artifacts (tests/unit/charts).

locals {
  # Workload images vendored by onboarder live under this ECR prefix; repos are
  # created on-demand by the job (like pull-through repos, they are NOT in TF state).
  # CLUSTER-SCOPED via resource_name_prefix (embeds random_id.postfix), mirroring the
  # vendored/* repos in images.tf: two deployments in one account/region must not share a
  # workload repo (the coexistence rule), and it keeps offboard/cleanup scoped to one cluster.
  workload_repo_prefix = "${local.resource_name_prefix}/workload"
  workload_repo_arn    = "arn:${data.aws_partition.current.partition}:ecr:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:repository/${local.workload_repo_prefix}/*"

  models_s3_uri     = "s3://${module.model_store.bucket_name}/${local.model_store_models_prefix}"
  rehost_in_s3_uri  = "s3://${module.model_store.bucket_name}/rehost/in"
  rehost_out_s3_uri = "s3://${module.model_store.bucket_name}/rehost/out"
  onboarder_name    = "${local.resource_name_prefix}-onboarder"
}

# Extra IAM for the onboard job: create+push workload/* ECR repos and WRITE the shared
# bucket (chart tarball in rehost/in, overrides out to rehost/out, weights to models/).
# READ access to weight-source buckets (any public/JumpStart source) comes from the
# AmazonS3ReadOnlyAccess managed policy attached below — a signed GetObject the onboarder
# needs even for a public bucket (a public-read ACL does NOT authorize a signed request
# from a principal lacking s3:GetObject). Cross-account sources still need a source bucket
# policy. S3ReadOnlyAccess also covers reads of our own bucket; this doc adds the WRITES.
data "aws_iam_policy_document" "onboarder_extra" {
  statement {
    sid       = "CreateWorkloadRepos"
    effect    = "Allow"
    actions   = ["ecr:CreateRepository", "ecr:DescribeRepositories", "ecr:TagResource"]
    resources = [local.workload_repo_arn]
  }
  statement {
    sid    = "WriteSharedBucket"
    effect = "Allow"
    # PutObject covers UploadPart/CompleteMultipartUpload; AbortMultipartUpload is the
    # multipart cleanup path the streaming weight copy uses on any part failure.
    actions   = ["s3:PutObject", "s3:AbortMultipartUpload"]
    resources = ["${module.model_store.bucket_arn}/*"]
  }
}

locals {
  # engine/onboarder.py is the single source of truth for the onboard logic (also
  # exercised by the unit tests, which import it directly). CodeBuild is NO_SOURCE, so
  # ship the module into the build by embedding it in the buildspec and decoding it at
  # runtime. gzip+base64 (not plain base64): the buildspec has a hard 25600-char limit
  # and the raw module base64-encodes past it — base64gzip keeps it ~9KB.
  onboarder_script_b64 = base64gzip(file("${path.module}/onboarder.py"))
}

module "onboarder" {
  source = "./modules/codebuild_job"

  project_name = local.onboarder_name
  # LARGE (16 GiB / 8 vCPU) suffices: the S3 weight copy is server-side (UploadPartCopy —
  # S3 moves the bytes internally), so the job holds no weight bytes in RAM and isn't
  # NIC-bound. A larger tier does NOT help — CodeBuild caps per-instance S3 bandwidth
  # regardless of vCPU/RAM (measured: XLARGE == 2XLARGE), and server-side copy sidesteps
  # that ceiling entirely. The rare byte-streaming fallback bounds itself to an 8 GiB part
  # buffer (see onboarder.py _S3_MEM_BUDGET_BYTES), which fits LARGE's 16 GiB.
  compute_type        = "BUILD_GENERAL1_LARGE"
  ecr_repository_arns = [local.workload_repo_arn]
  extra_policy_json   = data.aws_iam_policy_document.onboarder_extra.json
  attach_extra_policy = true
  # Read any weight-source bucket (public JumpStart cache, etc.) via the AWS-managed policy —
  # replaces the per-bucket onboard_weight_source_buckets allowlist.
  managed_policy_arns = ["arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonS3ReadOnlyAccess"]
  combined_tags       = local.combined_tags

  environment_variables = {
    ECR_REGISTRY    = local.ecr_registry
    WORKLOAD_PREFIX = local.workload_repo_prefix
    MODELS_S3_URI   = local.models_s3_uri
    REHOST_IN       = local.rehost_in_s3_uri
    REHOST_OUT      = local.rehost_out_s3_uri
    # Tags applied to every workload/* ECR repo the job creates (same tag set as all other
    # deployment resources) — so the repos are attributable + reapable by DeploymentId.
    # JSON-encoded map; onboarder.py decodes it into `aws ecr create-repository --tags`.
    RESOURCE_TAGS_JSON = jsonencode(local.combined_tags)
    # Overridden per start-build with the artifact to onboard — a Helm chart OR a KRO
    # graph, as an OCI ref, repo URL, or the tarball key uploaded to rehost/in. The
    # format is auto-detected from the unpacked dir; default keeps the project valid
    # standalone. (CHART_REF is kept as the name for backward compatibility.)
    CHART_REF = "unset"
  }

  # CodeBuild runs each command under /bin/sh (dash): no `set -o pipefail`. The onboard
  # script itself is bash (invoked explicitly), so its bash-isms are fine.
  buildspec = <<-YAML
    version: 0.2
    phases:
      install:
        commands:
          - |
            . /etc/os-release
            echo "deb https://download.opensuse.org/repositories/devel:/kubic:/libcontainers:/unstable/xUbuntu_$${VERSION_ID}/ /" > /etc/apt/sources.list.d/skopeo.list
            curl -fsSL "https://download.opensuse.org/repositories/devel:/kubic:/libcontainers:/unstable/xUbuntu_$${VERSION_ID}/Release.key" | gpg --dearmor -o /etc/apt/trusted.gpg.d/skopeo.gpg
            apt-get update -y && apt-get install -y skopeo
          - command -v helm >/dev/null 2>&1 || (curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash)
          - command -v s5cmd >/dev/null 2>&1 || (curl -fsSL https://github.com/peak/s5cmd/releases/download/v2.2.2/s5cmd_2.2.2_Linux-64bit.tar.gz | tar -xz -C /usr/local/bin s5cmd)
          - python3 -m pip install --quiet pyyaml
          - echo "${local.onboarder_script_b64}" | base64 -d | gunzip > /tmp/onboarder.py
      pre_build:
        commands:
          - aws ecr get-login-password --region $AWS_DEFAULT_REGION | skopeo login --username AWS --password-stdin $ECR_REGISTRY
      build:
        commands:
          # 1. Resolve the artifact to a local dir from CHART_REF (tarball in rehost/in,
          #    OCI ref, or a plain repo URL). A KRO graph ships as an s3:// tarball
          #    (graph.yaml + values.yaml); a chart may also be an OCI/http Helm ref.
          - mkdir -p /tmp/artifact
          - |
            case "$CHART_REF" in
              s3://*)          aws s3 cp "$CHART_REF" /tmp/artifact.tgz && tar -xzf /tmp/artifact.tgz -C /tmp/artifact --strip-components=1 ;;
              oci://*)         helm pull "$CHART_REF" --untar --untardir /tmp/artifact-oci && mv /tmp/artifact-oci/*/* /tmp/artifact/ ;;
              http://*|https://*) helm pull "$CHART_REF" --untar --untardir /tmp/artifact-oci && mv /tmp/artifact-oci/*/* /tmp/artifact/ ;;
              *)               echo "ERROR: CHART_REF must be an s3://, oci://, or http(s):// ref"; exit 1 ;;
            esac
          # 2. Run the onboard logic. It auto-detects chart (Chart.yaml -> overrides.yaml)
          #    vs graph (graph.yaml -> graph-air-gapped.yaml), pre-creates each workload/*
          #    ECR repo on demand, vendors images/weights, and runs the backstop.
          - CHART_DIR=/tmp/artifact OUT_DIR=/tmp python3 /tmp/onboarder.py
      post_build:
        commands:
          # 3. Publish the emitted artifact for the deployer. onboarder.py writes a
          #    result manifest naming what to publish (mode-agnostic: overrides.yaml or
          #    graph-air-gapped.yaml, under rehost/out/<name>/).
          - . /tmp/onboard-result.env
          - aws s3 cp "$ONBOARD_OUTPUT" "$REHOST_OUT/$ONBOARD_NAME/$ONBOARD_OUTPUT_BASENAME"
          - echo "[onboarder] published $ONBOARD_OUTPUT_BASENAME to $REHOST_OUT/$ONBOARD_NAME/"
  YAML
}
