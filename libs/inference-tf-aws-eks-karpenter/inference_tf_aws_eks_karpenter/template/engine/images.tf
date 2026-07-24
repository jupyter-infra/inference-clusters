# --- Image supply: ECR pull-through for platform images ---
#
# On the endpoints-only VPC a node has no public egress. Platform images
# reach nodes via ECR pull-through: the node pulls from PRIVATE ECR over the
# ecr.dkr/ecr.api + S3 endpoints; ECR fetches the upstream server-side (AWS IPs)
# on a cache miss and stores it by digest in our account. Every later pull is a
# cache hit — fully private.
#
# No-credentials-only: ECR offers anonymous pull-through rules for exactly
# three upstreams — ECR Public, the Kubernetes registry, and Quay.
# Docker Hub/GHCR/etc. require an ecr-pullthroughcache/ Secrets Manager secret,
# which we deliberately refuse to own.

# Each cached repo is namespaced under the rule's prefix, e.g.
# public.ecr.aws/karpenter/... -> <acct>.dkr.ecr.<region>…/ecr-public/karpenter/...

locals {
  # template-owned, NOT a jd variable — each entry is a wiring commitment (rule +
  # node-role IAM prefix + hosts.toml stanza). Object shape (not bare string)
  # leaves room for the future credential_arn seam without a schema change.
  trusted_upstreams = {
    ecr-public   = { url = "public.ecr.aws", prefix = "ecr-public" }
    quay         = { url = "quay.io", prefix = "quay" }
    registry-k8s = { url = "registry.k8s.io", prefix = "registry-k8s" }
  }

  # Repo-ARN prefixes ECR auto-creates on cache miss — the scope of the node
  # role's import grant and the registry pull-through policy.
  pullthrough_repo_arns = [
    for u in local.trusted_upstreams :
    "arn:${data.aws_partition.current.partition}:ecr:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:repository/${u.prefix}/*"
  ]

  # Base URI of our private registry — platform_*.tf compose full pull-through
  # image URIs from this + the upstream prefix (the PRIMARY resolution mechanism).
  # Exposed as a local here and as an output for consumers.
  ecr_registry = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${data.aws_region.current.id}.amazonaws.com"

  # Map each upstream host to its rule prefix, so a full upstream image path can
  # be rewritten to its pull-through URI by host-prefix substitution.
  upstream_prefix_by_host = { for k, u in local.trusted_upstreams : u.url => u.prefix }

  # common_images resolved to their pull-through URIs (single source of truth for
  # any chart/subchart referencing a utility image). Validation in
  # variables.tf guarantees each entry starts with a trusted host, so the split is
  # safe. e.g. public.ecr.aws/docker/library/busybox:1.36
  #        ->  <registry>/ecr-public/docker/library/busybox:1.36
  common_image_uris = {
    for img in var.common_images :
    img => "${local.ecr_registry}/${local.upstream_prefix_by_host[split("/", img)[0]]}/${join("/", slice(split("/", img), 1, length(split("/", img))))}"
  }
}

# The pull-through cache RULE and repository-creation TEMPLATE are account-regional
# shared singletons, not per-deployment resources — they are provisioned imperatively
# (create-if-absent / adopt-if-match / fail-on-divergence) in pullthrough.tf so two
# deployments in one account+region can coexist. See that file for the full rationale.

# Import-on-miss grant (the load-bearing allowlist). The node role's
# AmazonEC2ContainerRegistryReadOnly grants the pull (BatchGetImage/
# GetDownloadUrlForLayer) but NOT the import that pull-through performs on first
# reference. Scope those two actions to the trusted-upstream repo-ARN prefixes;
# a ref outside them fails closed (ImagePullBackOff) — this IS the platform-image
# allowlist. Identity policy on the node role (not the ECR resource policy).
data "aws_iam_policy_document" "node_pullthrough" {
  statement {
    sid    = "PullThroughImportOnMiss"
    effect = "Allow"
    actions = [
      "ecr:CreateRepository",
      "ecr:BatchImportUpstreamImage",
      # Import can fail without TagResource when ECR auto-tags the created repo
      # (per the ecr-pull-through-cache blueprint). Scoped to the same prefixes.
      "ecr:TagResource",
    ]
    resources = local.pullthrough_repo_arns
  }
}

resource "aws_iam_role_policy" "node_pullthrough" {
  name   = "${local.resource_name_prefix}-node-pullthrough"
  role   = module.node_role.role_name
  policy = data.aws_iam_policy_document.node_pullthrough.json
}

# Note: the node IDENTITY policy above is the sufficient, documented grant for
# import-on-miss. AWS: "if an IAM entity has more permissions granted by an IAM
# policy than the registry permissions policy is granting, the IAM policy takes
# precedence" (pull-through-cache-iam). An additional aws_ecr_registry_policy
# would be redundant — and an earlier attempt failed PutRegistryPolicy with
# "Invalid registry policy provided" — so we deliberately do NOT set one.

# Barrier: "a platform image has a reachable pull path" = the shared pull-through rules +
# creation templates (pullthrough.tf) and the node import IAM exist. Every helm_release and
# the bootstrap NG depend_on this so nothing schedules before pull-through can serve an image.
resource "null_resource" "pullthrough_ready" {
  depends_on = [
    null_resource.pullthrough_infra,
    aws_iam_role_policy.node_pullthrough,
  ]
}

# --- Vendored images (the OTHER supply path) ---
#
# Pull-through (above) covers images on the three no-creds upstreams. Images that
# live ONLY on registries pull-through can't proxy (nvcr.io) or that require
# credentials our no-creds pull-through refuses (docker.io/ghcr.io) are instead
# mirrored into our own ECR via a server-side CodeBuild job. The copy runs in
# CodeBuild (public egress) — NOT on the jd host and NOT on a cluster node — so nodes
# stay air-gapped and pull the vendored copy from private ECR like any other image.
# This is the workload-vendoring engine pulled forward for the handful of
# platform images with no no-creds home. Add an entry → it gets an ECR repo, IAM
# scope, and a build trigger automatically.

# The EFA device-plugin image lives ONLY on the EKS-managed regional ECR (it is
# NOT on public.ecr.aws, so pull-through can't proxy it). That registry's account
# is region-specific (one account covers most regions, a distinct account for ~15
# opt-in/newer ones). Rather than hardcode a region→account map, we INFER the
# registry from an add-on EKS already installed: the vpc-cni (aws-node) DaemonSet's
# image is <account>.dkr.ecr.<region>.amazonaws.com/amazon-k8s-cni:<tag> — whatever
# EKS resolved for THIS region/partition. The EFA image sits on the same registry,
# so we vendor it from there into our own ECR (like every other platform image),
# and nodes pull the private copy. Only read/vendored when EFA is enabled.
data "kubernetes_resource" "aws_node" {
  count       = var.enable_efa ? 1 : 0
  api_version = "apps/v1"
  kind        = "DaemonSet"

  metadata {
    name      = "aws-node"
    namespace = "kube-system"
  }

  # cluster_addons is the "providers authorized + all add-ons up" barrier: it
  # aggregates vpc-cni (so its DaemonSet image is readable) and the access
  # associations/entry (so the kubernetes provider can authenticate). depends_on
  # defers the read to apply, so eks_ecr_registry is unknown at plan — fine, it
  # only feeds a null_resource trigger (apply-time), never a for_each key.
  depends_on = [null_resource.cluster_addons]

  # Guard the inferred registry BEFORE it flows into the vendoring source + the
  # cross-account IAM grant. The derived string is only trustworthy if it is the
  # canonical EKS regional ECR host for THIS region: <12-digit-account>.dkr.ecr.
  # <region>.amazonaws.com. Reject anything else (a sidecar image at some other
  # index, a non-ECR ref, a foreign region/partition) so we never vendor from —
  # or grant pull on — an unexpected registry. Runs at apply (data source is
  # deferred), failing the apply loudly rather than vendoring a surprise image.
  lifecycle {
    postcondition {
      condition = can(regex(
        "^[0-9]{12}\\.dkr\\.ecr\\.${data.aws_region.current.id}\\.amazonaws\\.com$",
        split("/", one([for c in self.object.spec.template.spec.containers : c.image if c.name == "aws-node"]))[0]
      ))
      error_message = "Inferred EKS ECR registry from the aws-node DaemonSet is not the expected <account>.dkr.ecr.${data.aws_region.current.id}.amazonaws.com host; refusing to vendor the EFA image from it."
    }
  }
}

locals {
  # <account>.dkr.ecr.<region>.amazonaws.com — the EKS-managed regional registry,
  # inferred from the vpc-cni image (see above). We select the CNI container by
  # NAME (aws-node), not index 0, so an injected sidecar at index 0 can't be
  # mistaken for it; the postcondition on the data source has already asserted the
  # derived host matches the canonical shape. Empty when EFA is disabled (the data
  # source isn't read then, and nothing references this).
  eks_ecr_registry = var.enable_efa ? split("/", one([for c in data.kubernetes_resource.aws_node[0].object.spec.template.spec.containers : c.image if c.name == "aws-node"]))[0] : ""

  # source (pinned upstream ref) → our ECR repo. Keys are stable (renaming one
  # replaces its repo). value.repo is the LOGICAL repo suffix; the actual ECR repo
  # name is prefixed with resource_name_prefix (below) so two deployments in the same
  # account+region get distinct repos and never collide on create/force_delete.
  base_vendored_images = {
    # nvcr.io — no no-creds mirror at all.
    device_plugin = {
      repo   = "gpu/k8s-device-plugin"
      source = "nvcr.io/nvidia/k8s-device-plugin:${var.nvidia_device_plugin_version}"
    }
    # nvcr.io — DCGM is NOT on Quay/ECR-Public either (verified).
    dcgm_exporter = {
      repo   = "gpu/dcgm-exporter"
      source = "nvcr.io/nvidia/k8s/dcgm-exporter:${var.nvidia_dcgm_exporter_version}"
    }
    # docker.io — Grafana publishes ONLY to Docker Hub + ghcr, neither a no-creds
    # pull-through upstream (verified: quay.io/grafana/grafana 401s). Docker Hub
    # allows anonymous pulls from CodeBuild's public egress, so we vendor it.
    grafana = {
      repo   = "vendored/grafana"
      source = "docker.io/grafana/grafana:${var.grafana_version}"
    }
    # ghcr.io — KEDA's three images are ghcr-only (verified: NOT on Quay (401) or
    # ECR-Public (404); the plan's "pin to quay.io/kedacore/keda" was wrong, same
    # stale-registry class as Grafana/DCGM). ghcr allows anonymous pulls from
    # CodeBuild's public egress, so vendor all three at the chart appVersion tag.
    keda_operator = {
      repo   = "vendored/keda"
      source = "ghcr.io/kedacore/keda:${var.keda_chart_version}"
    }
    keda_metrics_apiserver = {
      repo   = "vendored/keda-metrics-apiserver"
      source = "ghcr.io/kedacore/keda-metrics-apiserver:${var.keda_chart_version}"
    }
    keda_admission_webhooks = {
      repo   = "vendored/keda-admission-webhooks"
      source = "ghcr.io/kedacore/keda-admission-webhooks:${var.keda_chart_version}"
    }
  }

  # EFA device plugin — vendored from the inferred EKS regional ECR (see the
  # kubernetes_resource above), NOT a hardcoded account. Merged in only when EFA
  # is enabled: the source references eks_ecr_registry (empty/unknown otherwise).
  # The repo path `eks/aws-efa-k8s-device-plugin` is the stable EKS convention;
  # the tag is the chart's appVersion (which the chart also defaults image.tag to).
  efa_vendored_images = var.enable_efa ? {
    efa_device_plugin = {
      repo   = "vendored/aws-efa-k8s-device-plugin"
      source = "${local.eks_ecr_registry}/eks/aws-efa-k8s-device-plugin:${var.efa_device_plugin_image_tag}"
    }
  } : {}

  vendored_images = merge(local.base_vendored_images, local.efa_vendored_images)

  vendored_tag = "vendored"
}

# ECR repos that receive the vendored images (in Terraform state, unlike the
# pull-through auto-created repos).
resource "aws_ecr_repository" "vendored" {
  for_each = local.vendored_images

  name                 = "${local.resource_name_prefix}/${each.value.repo}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = local.combined_tags
}

# Cross-account SOURCE read for the EFA image. The vendor job pulls the EFA image
# from the EKS-managed regional ECR (a DIFFERENT account — the inferred registry).
# ECR cross-account pull needs BOTH the source repo's resource policy (EKS grants
# all accounts) AND an identity policy on the caller (the CodeBuild role). Its base
# ECRPush policy is scoped to OUR vendored repos only, so grant read on the EKS
# `eks/*` repos here. The account is wildcarded (arn ...:*:repository/eks/*) so this
# works for the inferred registry in ANY region/partition — no hardcoded account.
# Only attached when EFA is enabled (the only cross-account source we vendor).
data "aws_iam_policy_document" "efa_source_pull" {
  statement {
    sid    = "PullEksManagedEfaImage"
    effect = "Allow"
    actions = [
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchCheckLayerAvailability",
    ]
    resources = ["arn:${data.aws_partition.current.partition}:ecr:${data.aws_region.current.id}:*:repository/eks/*"]
  }
}

# --- CodeBuild vendoring job (upstream → our ECR, server-side) ---
#
# One project, driven per-image via start-build env overrides.
module "image_vendor" {
  source = "./modules/codebuild_job"

  project_name        = "${local.resource_name_prefix}-image-vendor"
  ecr_repository_arns = [for r in aws_ecr_repository.vendored : r.arn]
  combined_tags       = local.combined_tags

  # Cross-account read of the EKS-managed EFA source image (see above). Gated on the
  # plan-time-known enable_efa flag (attach_extra_policy must not depend on JSON).
  attach_extra_policy = var.enable_efa
  extra_policy_json   = data.aws_iam_policy_document.efa_source_pull.json

  # AWS_DEFAULT_REGION is a CodeBuild built-in — no need to pass it.
  environment_variables = {
    ECR_REGISTRY = local.ecr_registry
    # Overridden per start-build; defaults keep the project valid standalone.
    SRC_IMAGE = "unset"
    DST_IMAGE = "unset"
  }

  # Phase-structured buildspec (eks-oidc application module style). CodeBuild runs
  # each command under /bin/sh (dash): plain pipes work, but `set -o pipefail` is
  # illegal — so we simply don't use it. A non-zero exit fails the build.
  #
  # NO `--all`: it copies the whole manifest list including SBOM/attestation layers
  # (application/vnd.in-toto+json), which the packaged skopeo (1.4.1) can't handle —
  # DCGM's image carries one and `--all` fails "unsupported MIME type" (verified live).
  # Omitting it copies the CodeBuild host's platform (linux/amd64), which is all
  # our x86_64 nodes need.
  buildspec = <<-YAML
    version: 0.2
    phases:
      pre_build:
        commands:
          - command -v skopeo >/dev/null 2>&1 || (apt-get update -y && apt-get install -y skopeo)
          - ECR_PASSWORD=$(aws ecr get-login-password --region $AWS_DEFAULT_REGION)
          - echo "$ECR_PASSWORD" | skopeo login --username AWS --password-stdin $ECR_REGISTRY
          # If the SOURCE is an ECR registry (e.g. the EKS-managed regional ECR the
          # EFA image lives on, possibly a different account), log in there too. The
          # same regional token authorizes cross-account pull when that repo's
          # resource policy allows it (as EKS's managed repos do for all accounts).
          - SRC_REGISTRY=$(echo "$SRC_IMAGE" | cut -d/ -f1)
          - case "$SRC_REGISTRY" in *.dkr.ecr.*.amazonaws.com) echo "$ECR_PASSWORD" | skopeo login --username AWS --password-stdin "$SRC_REGISTRY" ;; esac
      build:
        commands:
          - skopeo copy "docker://$SRC_IMAGE" "docker://$DST_IMAGE"
  YAML
}

# Trigger the vendor build per image and wait for it (start-build + poll on the jd
# host — NOT the image transfer, which runs in CodeBuild). Retries once on failure.
resource "null_resource" "image_vendor" {
  for_each = local.vendored_images

  triggers = {
    source  = each.value.source
    dest    = "${aws_ecr_repository.vendored[each.key].repository_url}:${local.vendored_tag}"
    project = module.image_vendor.project_name
    region  = data.aws_region.current.id
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      run_build() {
        local BUILD_ID
        BUILD_ID=$(aws codebuild start-build \
          --project-name ${self.triggers.project} \
          --region ${self.triggers.region} \
          --environment-variables-override \
            "name=SRC_IMAGE,value=${self.triggers.source},type=PLAINTEXT" \
            "name=DST_IMAGE,value=${self.triggers.dest},type=PLAINTEXT" \
          --query 'build.id' --output text)

        echo "[image-vendor] started $BUILD_ID for ${self.triggers.source}"
        SECONDS=0
        TIMEOUT=1800

        while true; do
          if [ $SECONDS -ge $TIMEOUT ]; then
            echo "[image-vendor] ERROR: build timed out after 30m."
            return 1
          fi
          STATUS=$(aws codebuild batch-get-builds \
            --ids "$BUILD_ID" --region ${self.triggers.region} \
            --query 'builds[0].buildStatus' --output text)
          case "$STATUS" in
            SUCCEEDED) echo "[image-vendor] $BUILD_ID succeeded in $(($SECONDS / 60))m $(($SECONDS % 60))s."; return 0 ;;
            FAILED|FAULT|STOPPED|TIMED_OUT) echo "[image-vendor] $BUILD_ID ended: $STATUS"; return 1 ;;
            *) sleep 15 ;;
          esac
        done
      }

      if ! run_build; then
        echo "[image-vendor] first attempt failed, retrying in 60s..."
        sleep 60
        run_build
      fi
    EOT
  }

  depends_on = [module.image_vendor]
}
