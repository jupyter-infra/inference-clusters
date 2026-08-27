# Image supply — pull-through for the three no-creds upstreams (public.ecr.aws,
# quay.io, registry.k8s.io); vendored copies (CodeBuild → private ECR) for
# images whose only home is docker.io/ghcr.io/nvcr.io.

locals {
  trusted_upstreams = {
    ecr-public   = { url = "public.ecr.aws", prefix = "ecr-public" }
    quay         = { url = "quay.io", prefix = "quay" }
    registry-k8s = { url = "registry.k8s.io", prefix = "registry-k8s" }
  }

  pullthrough_repo_arns = [
    for u in local.trusted_upstreams :
    "arn:${data.aws_partition.current.partition}:ecr:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:repository/${u.prefix}/*"
  ]

  ecr_registry = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${data.aws_region.current.id}.amazonaws.com"

  upstream_prefix_by_host = { for k, u in local.trusted_upstreams : u.url => u.prefix }

  # `var.common_images` rewritten to private-registry URIs by host-prefix
  # substitution. e.g. public.ecr.aws/docker/library/busybox:1.36
  #                 -> <registry>/ecr-public/docker/library/busybox:1.36
  common_image_uris = {
    for img in var.common_images :
    img => "${local.ecr_registry}/${local.upstream_prefix_by_host[split("/", img)[0]]}/${join("/", slice(split("/", img), 1, length(split("/", img))))}"
  }
}

# Import-on-miss grant. `AmazonEC2ContainerRegistryReadOnly` allows the pull but
# NOT the auto-import that pull-through performs on first reference — scope the
# two extra actions to the trusted-upstream prefixes so any ref outside them
# fails closed (ImagePullBackOff). This IS the platform-image allowlist.
data "aws_iam_policy_document" "node_pullthrough" {
  statement {
    sid    = "PullThroughImportOnMiss"
    effect = "Allow"
    actions = [
      "ecr:CreateRepository",
      "ecr:BatchImportUpstreamImage",
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

# Barrier for every consumer (helm_release / bootstrap NG): pull-through
# infrastructure exists AND the node role can import-on-miss.
resource "null_resource" "pullthrough_ready" {
  depends_on = [
    null_resource.pullthrough_infra,
    aws_iam_role_policy.node_pullthrough,
  ]
}

# --- Vendored images ---
#
# The EFA device plugin lives ONLY on the EKS-managed regional ECR (region-
# dependent account). Infer it from the aws-node DaemonSet image; the
# postcondition rejects anything that isn't the canonical <account>.dkr.ecr.
# <region>.amazonaws.com host before it flows into IAM or vendoring.
data "kubernetes_resource" "aws_node" {
  count       = var.enable_efa ? 1 : 0
  api_version = "apps/v1"
  kind        = "DaemonSet"

  metadata {
    name      = "aws-node"
    namespace = "kube-system"
  }

  depends_on = [null_resource.cluster_addons]

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
  eks_ecr_registry = var.enable_efa ? split("/", one([for c in data.kubernetes_resource.aws_node[0].object.spec.template.spec.containers : c.image if c.name == "aws-node"]))[0] : ""

  base_vendored_images = {
    device_plugin = {
      repo   = "gpu/k8s-device-plugin"
      source = "nvcr.io/nvidia/k8s-device-plugin:${var.nvidia_device_plugin_version}"
    }
    dcgm_exporter = {
      repo   = "gpu/dcgm-exporter"
      source = "nvcr.io/nvidia/k8s/dcgm-exporter:${var.nvidia_dcgm_exporter_version}"
    }
    grafana = {
      repo   = "vendored/grafana"
      source = "docker.io/grafana/grafana:${var.grafana_version}"
    }
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

  efa_vendored_images = var.enable_efa ? {
    efa_device_plugin = {
      repo   = "vendored/aws-efa-k8s-device-plugin"
      source = "${local.eks_ecr_registry}/eks/aws-efa-k8s-device-plugin:${var.efa_device_plugin_image_tag}"
    }
  } : {}

  vendored_images = merge(local.base_vendored_images, local.efa_vendored_images)

  vendored_tag = "vendored"
}

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

# Cross-account read of the EKS-managed EFA source image (region-specific
# account). Wildcarded on account so it works in any region/partition.
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

module "image_vendor" {
  source = "./modules/codebuild_job"

  project_name        = "${local.resource_name_prefix}-image-vendor"
  ecr_repository_arns = [for r in aws_ecr_repository.vendored : r.arn]
  combined_tags       = local.combined_tags

  attach_extra_policy = var.enable_efa
  extra_policy_json   = data.aws_iam_policy_document.efa_source_pull.json

  environment_variables = {
    ECR_REGISTRY = local.ecr_registry
    SRC_IMAGE    = "unset"
    DST_IMAGE    = "unset"
  }

  # skopeo from Kubic OBS: Ubuntu-packaged 1.4.1 has a blob-existence-check auth
  # bug that kills every multi-arch vendor build (grafana, nvidia, keda, dcgm).
  # Kubic ships >=1.15 with a shared auth store.
  #
  # NO `--all`: source manifest lists include SBOM/attestation layers; single-
  # arch copy of the CodeBuild host's platform is all x86_64 nodes need.
  buildspec = <<-YAML
    version: 0.2
    phases:
      pre_build:
        commands:
          - |
            . /etc/os-release
            echo "deb https://download.opensuse.org/repositories/devel:/kubic:/libcontainers:/unstable/xUbuntu_$${VERSION_ID}/ /" > /etc/apt/sources.list.d/skopeo.list
            curl -fsSL "https://download.opensuse.org/repositories/devel:/kubic:/libcontainers:/unstable/xUbuntu_$${VERSION_ID}/Release.key" | gpg --dearmor -o /etc/apt/trusted.gpg.d/skopeo.gpg
            apt-get update -y && apt-get install -y skopeo
          - ECR_PASSWORD=$(aws ecr get-login-password --region $AWS_DEFAULT_REGION)
          - echo "$ECR_PASSWORD" | skopeo login --username AWS --password-stdin $ECR_REGISTRY
          - SRC_REGISTRY=$(echo "$SRC_IMAGE" | cut -d/ -f1)
          - case "$SRC_REGISTRY" in *.dkr.ecr.*.amazonaws.com) echo "$ECR_PASSWORD" | skopeo login --username AWS --password-stdin "$SRC_REGISTRY" ;; esac
      build:
        commands:
          - skopeo copy "docker://$SRC_IMAGE" "docker://$DST_IMAGE"
  YAML
}

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
