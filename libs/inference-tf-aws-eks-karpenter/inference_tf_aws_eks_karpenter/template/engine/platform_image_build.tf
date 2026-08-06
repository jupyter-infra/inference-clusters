# === Image-build job — build a source dir into the cluster's private ECR ===
#
# A consumer invokes this AFTER the cluster is up (NOT a `jd up` step) to build a
# container image whose source lives in a SEPARATE repo — for images that have no
# published upstream to import (skopeo copy / pull-through only mirror EXISTING
# images; this fills the "build one that doesn't exist yet" gap).
#
# Cross-repo by design, so the source is a RUNTIME input, never terraform state
# (mirrors the onboarder's CHART_REF contract, NOT the eks-oidc app module's
# terraform-time archive_file/aws_s3_object — that only works when the source dir
# is part of the same template's TF). Flow:
#   1. consumer tars its source dir (Dockerfile + any wheels/context) and uploads
#      to s3://<store>/image-build/in/<name>/source.tgz
#   2. consumer runs `aws codebuild start-build` with SOURCE_REF + IMAGE_NAME + TAG
#   3. this job: s3 cp the tarball, unpack, `docker build`, push to
#      <ecr>/workload/<name>:<tag> (+ :latest)
# Downstream the result is an ordinary workload image, imported/pulled like any other.
#
# Build runs in CodeBuild (public egress, where pip/apt/base-image pulls work) — NOT
# on the air-gapped cluster — so nodes still only ever pull the finished private image.
#
# A third instance of modules/codebuild_job (alongside onboarder + image_vendor):
# NO_SOURCE + env-driven, privileged_mode (module default) for the Docker daemon,
# workload/* ECR push scope. Arbitrary source size (dir + wheels) rides an S3 object,
# not an env var — no buildspec size limit.

locals {
  # Build-input prefix on the shared store bucket where consumers upload source tarballs.
  image_build_in_s3_uri = "s3://${module.model_store.bucket_name}/image-build/in"
  image_build_name      = "${local.resource_name_prefix}-image-build"
}

# Extra IAM: create-on-demand + push the workload/* repo the built image lands in
# (mirrors the onboarder's CreateWorkloadRepos grant — repos are not in TF state),
# and READ the build-input prefix the consumer uploaded the source tarball to.
data "aws_iam_policy_document" "image_build_extra" {
  statement {
    sid       = "CreateWorkloadRepos"
    effect    = "Allow"
    actions   = ["ecr:CreateRepository", "ecr:DescribeRepositories", "ecr:TagResource"]
    resources = [local.workload_repo_arn]
  }
  statement {
    sid       = "ReadBuildSource"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${module.model_store.bucket_arn}/image-build/in/*"]
  }
  statement {
    sid       = "ListBuildSource"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [module.model_store.bucket_arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["image-build/in/*"]
    }
  }
}

module "image_build" {
  source = "./modules/codebuild_job"

  project_name = local.image_build_name
  # Push target: the same workload/* repos the onboarder vendors into.
  ecr_repository_arns = [local.workload_repo_arn]
  extra_policy_json   = data.aws_iam_policy_document.image_build_extra.json
  attach_extra_policy = true
  # docker build of a slim base + pip install is CPU/IO light; MEDIUM gives wheel
  # builds a little headroom without over-provisioning.
  compute_type  = "BUILD_GENERAL1_MEDIUM"
  combined_tags = local.combined_tags

  environment_variables = {
    ECR_REGISTRY       = local.ecr_registry
    WORKLOAD_PREFIX    = local.workload_repo_prefix
    RESOURCE_TAGS_JSON = jsonencode(local.combined_tags)
    # Overridden per start-build:
    #   SOURCE_REF — s3:// URI of the source tarball (Dockerfile + build context)
    #   IMAGE_NAME — workload repo suffix, e.g. "aiperf" -> <ecr>/workload/aiperf
    #   IMAGE_TAG  — version tag, e.g. "0.9.0"
    SOURCE_REF = "unset"
    IMAGE_NAME = "unset"
    IMAGE_TAG  = "unset"
  }

  # CodeBuild runs each command under /bin/sh (dash): no `set -o pipefail`.
  # privileged_mode (module default) provides the Docker daemon. Commands are
  # QUOTED scalars — an unquoted colon-space (e.g. cut -d:) is misparsed by the
  # buildspec YAML reader as a mapping.
  buildspec = <<-YAML
    version: 0.2
    phases:
      pre_build:
        commands:
          - 'aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin $ECR_REGISTRY'
          - 'DST_REPO="$WORKLOAD_PREFIX/$IMAGE_NAME"'
          # Create the repo (idempotent) WITH the deployment tags, so imperatively-created
          # workload/* repos are attributable + reapable by DeploymentId (matches the
          # onboarder; the only cleanup handle since these repos are not in TF state).
          - 'TAG_ARGS=$(echo "$RESOURCE_TAGS_JSON" | python3 -c "import json,sys; t=json.load(sys.stdin); print(chr(32).join(f\"Key={k},Value={v}\" for k,v in t.items()))")'
          - 'aws ecr describe-repositories --repository-names "$DST_REPO" --region $AWS_DEFAULT_REGION >/dev/null 2>&1 || aws ecr create-repository --repository-name "$DST_REPO" --region $AWS_DEFAULT_REGION --tags $TAG_ARGS >/dev/null 2>&1 || true'
      build:
        commands:
          - 'mkdir -p /tmp/src'
          - 'aws s3 cp "$SOURCE_REF" /tmp/source.tgz'
          - 'tar -xzf /tmp/source.tgz -C /tmp/src'
          - 'test -f /tmp/src/Dockerfile || { echo "ERROR: no Dockerfile at root of source tarball"; exit 1; }'
          - 'DST="$ECR_REGISTRY/$WORKLOAD_PREFIX/$IMAGE_NAME:$IMAGE_TAG"'
          - 'docker build --platform linux/amd64 -t "$DST" -f /tmp/src/Dockerfile /tmp/src'
          - 'docker push "$DST"'
      post_build:
        commands:
          - 'echo "[image-build] published $ECR_REGISTRY/$WORKLOAD_PREFIX/$IMAGE_NAME:$IMAGE_TAG"'
  YAML
}
