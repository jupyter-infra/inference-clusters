# === SageMaker HyperPod Inference Gateway (opt-in) ===
#
# Installs the `amazon-sagemaker-hyperpod-inference` EKS add-on with BOTH of its
# components on:
#   - inferenceOperator — reconciles InferenceEndpointConfig / JumpStartModel CRs;
#   - inferenceGateway  — the LLM-aware routing layer: Envoy Gateway -> Body-Based Router
#     (BBR, reads the OpenAI body `model` field) -> per-model Endpoint Picker (llm-d EPP)
#     scoring pods on queue depth / KV-cache / prefix affinity.
# and a default InferenceGatewayConfig (charts/hyperpod-gateway) that KRO tracks and
# plain Deployments attach to by labeling their model pods (see the chart README).
#
# Everything here is gated on var.enable_hyperpod_inference_gateway (off by default).
#
# What the add-on needs that the base template does not already provide:
#   - IRSA, not Pod Identity. The add-on's podIdentityConfiguration is empty and every
#     role it takes (executionRoleArn, inferenceGateway.serviceAccount.roleArn) is
#     annotated onto its service accounts as eks.amazonaws.com/role-arn. The base template
#     has no IAM OIDC provider (it uses Pod Identity everywhere), so one is created here.
#   - a TLS-certificate S3 bucket (`tlsCertificateS3Bucket`, required by the schema). It is
#     named hyperpod-tls-<postfix>-* because the AWS-managed execution policy
#     (AmazonSageMakerHyperPodInferenceAccess) only grants S3 on `hyperpod-tls*` buckets.
#   - cert-manager (the operator's webhook certs + the gateway's TLS auto-issue path).
#   - the FSx CSIDriver + the AWS Load Balancer Controller CRDs: the operator's
#     `check-csi-drivers` init container hard-requires fsx.csi.aws.com, s3.csi.aws.com and
#     the elbv2.k8s.aws / keda.sh / cert-manager.io CRDs and crash-loops without them
#     (verified live). FSx comes from the aws-fsx-csi-driver add-on unless enable_fsx already
#     installs the driver; the ALB CRDs come from the add-on's own bundled ALB controller.
#   - extra interface endpoints on the endpoints-only VPC: sagemaker.api (operator ->
#     SageMaker endpoint registration / HyperPod describe) and acm (TLS import).
#
# Deliberately DISABLED add-on sub-component:
#   - keda: the template already installs KEDA (platform_keda.tf) cluster-wide; a second
#     KEDA would fight over the keda.sh CRDs and the external-metrics APIService.
# The bundled ALB controller stays ON (the operator requires its CRDs) but nothing here
# creates an Ingress, so it provisions no load balancer; the gateway is reached in-VPC
# through its Envoy Service (see outputs).

locals {
  hyperpod_gw_enabled = var.enable_hyperpod_inference_gateway

  # Namespace + service-account names fixed by the add-on (not configurable). The IRSA
  # trust subjects below MUST match these exactly or AssumeRoleWithWebIdentity is denied.
  hyperpod_inference_namespace   = "hyperpod-inference-system"
  hyperpod_operator_sa           = "hyperpod-inference-controller-manager"
  hyperpod_gateway_controller_sa = "inference-gateway-controller"
  hyperpod_alb_controller_sa     = "aws-load-balancer-controller"

  # The default InferenceGatewayConfig name (one IGC == one Envoy Gateway). Lives in the
  # workload namespace because each scheduler's InferencePool selects model pods in the
  # IGC's own namespace.
  hyperpod_gateway_config_name = "inference-gateway"

  # Pod label a model Deployment (or KRO track) carries to be routed by scheduler <name>.
  # Single source of truth for the chart, the KRO starter RGD, outputs and docs.
  hyperpod_gateway_scheduler_label = "inference/gateway-scheduler"

  # Interface endpoints the add-on needs on the endpoints-only VPC that the base VPC
  # module does not already create (sts/eks/ecr/elasticloadbalancing/logs exist there).
  hyperpod_gateway_endpoints = local.hyperpod_gw_enabled ? toset(["sagemaker.api", "acm"]) : toset([])

  oidc_issuer_host = replace(module.eks_cluster.oidc_issuer_url, "https://", "")
}

# --- Endpoints-only VPC: extra interface endpoints ---
resource "aws_vpc_endpoint" "hyperpod_gateway" {
  for_each = local.hyperpod_gateway_endpoints

  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${data.aws_region.current.id}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnet_ids
  security_group_ids  = [module.vpc.endpoint_security_group_id]
  private_dns_enabled = true

  tags = merge(local.combined_tags, {
    Name = "${local.resource_name_prefix}-vpce-${each.value}"
  })
}

# --- IRSA: IAM OIDC provider for this cluster's issuer ---
#
# Per-cluster (the issuer URL embeds the cluster's unique OIDC id), so two deployments
# never collide. thumbprint_list is omitted: IAM validates EKS issuers against its own
# trusted CA store.
resource "aws_iam_openid_connect_provider" "eks" {
  count = local.hyperpod_gw_enabled ? 1 : 0

  url            = module.eks_cluster.oidc_issuer_url
  client_id_list = ["sts.amazonaws.com"]
  tags           = local.combined_tags
}

# Trust: the operator role is also assumed by SageMaker (endpoint registration), per the
# add-on docs; the gateway-controller role is IRSA-only.
data "aws_iam_policy_document" "hyperpod_operator_trust" {
  count = local.hyperpod_gw_enabled ? 1 : 0

  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["sagemaker.amazonaws.com"]
    }
  }

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.eks[0].arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:sub"
      values   = ["system:serviceaccount:${local.hyperpod_inference_namespace}:${local.hyperpod_operator_sa}"]
    }
  }
}

data "aws_iam_policy_document" "hyperpod_gateway_controller_trust" {
  count = local.hyperpod_gw_enabled ? 1 : 0

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.eks[0].arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:sub"
      values   = ["system:serviceaccount:${local.hyperpod_inference_namespace}:${local.hyperpod_gateway_controller_sa}"]
    }
  }
}

# Execution role (add-on `executionRoleArn`): AWS-managed policy scoped by AWS to what
# the operator does (ALB/ACM/S3 on hyperpod-tls* buckets/SageMaker endpoint registration).
module "hyperpod_operator_role" {
  count = local.hyperpod_gw_enabled ? 1 : 0

  source             = "./modules/iam_role"
  role_name          = "${local.resource_name_prefix}-hp-inference-exec"
  assume_role_policy = data.aws_iam_policy_document.hyperpod_operator_trust[0].json
  policy_arns        = ["arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSageMakerHyperPodInferenceAccess"]
  combined_tags      = local.combined_tags
}

# The operator calls sagemaker:UpdateClusterInference (EnableClusterInference) on the
# attached HyperPod cluster at start-up, passing ITS OWN execution role to SageMaker. The
# managed policy does not grant iam:PassRole, so without this the operator crash-loops with
# AccessDenied on iam:PassRole (verified live). Least privilege: only this role, only to
# SageMaker.
data "aws_iam_policy_document" "hyperpod_operator_pass_role" {
  count = local.hyperpod_gw_enabled ? 1 : 0

  statement {
    sid       = "PassOwnExecutionRoleToSageMaker"
    actions   = ["iam:PassRole"]
    resources = [module.hyperpod_operator_role[0].role_arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["sagemaker.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "hyperpod_operator_pass_role" {
  count = local.hyperpod_gw_enabled ? 1 : 0

  name   = "${local.resource_name_prefix}-hp-inference-exec-passrole"
  role   = module.hyperpod_operator_role[0].role_name
  policy = data.aws_iam_policy_document.hyperpod_operator_pass_role[0].json
}

# Gateway-controller role (add-on `inferenceGateway.serviceAccount.roleArn`): ACM import/
# tag/describe/delete for the TLS auto-issue flow. The docs require it to exist BEFORE the
# add-on installs (the controller reads it at start-up).
module "hyperpod_gateway_controller_role" {
  count = local.hyperpod_gw_enabled ? 1 : 0

  source             = "./modules/iam_role"
  role_name          = "${local.resource_name_prefix}-hp-inference-gw"
  assume_role_policy = data.aws_iam_policy_document.hyperpod_gateway_controller_trust[0].json
  policy_arns        = ["arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSageMakerHyperPodInferenceGatewayAccess"]
  combined_tags      = local.combined_tags
}

data "aws_iam_policy_document" "hyperpod_alb_controller_trust" {
  count = local.hyperpod_gw_enabled ? 1 : 0

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.eks[0].arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:sub"
      values   = ["system:serviceaccount:${local.hyperpod_inference_namespace}:${local.hyperpod_alb_controller_sa}"]
    }
  }
}

# Bundled ALB controller role (add-on `alb.serviceAccount.roleArn`). The policy is the
# AWS Load Balancer Controller policy the add-on documents (vendored verbatim from
# aws/sagemaker-hyperpod-cli inference-operator/migration/alb-controller-policy.json).
module "hyperpod_alb_controller_role" {
  count = local.hyperpod_gw_enabled ? 1 : 0

  source             = "./modules/iam_role"
  role_name          = "${local.resource_name_prefix}-hp-inference-alb"
  assume_role_policy = data.aws_iam_policy_document.hyperpod_alb_controller_trust[0].json
  combined_tags      = local.combined_tags
}

resource "aws_iam_role_policy" "hyperpod_alb_controller" {
  count = local.hyperpod_gw_enabled ? 1 : 0

  name   = "${local.resource_name_prefix}-hp-inference-alb"
  role   = module.hyperpod_alb_controller_role[0].role_name
  policy = file("${path.module}/policies/hyperpod-alb-controller-policy.json")
}

# --- TLS certificate bucket (add-on `tlsCertificateS3Bucket`) ---
#
# Prefix MUST start with `hyperpod-tls` (managed-policy scope) and embeds the random_id
# postfix so two deployments never collide; bucket_prefix adds a unique suffix.
module "hyperpod_tls_bucket" {
  count = local.hyperpod_gw_enabled ? 1 : 0

  source             = "./modules/s3_bucket"
  bucket_name_prefix = "hyperpod-tls-${random_id.postfix.hex}-"
  combined_tags      = local.combined_tags
  lifecycle_rule     = null
}

# --- cert-manager (dependency add-on) ---
#
# Required by the inference operator (webhook serving certs) and by the gateway's TLS
# auto-issue path. EKS community add-on, images from the regional EKS add-on ECR (reached
# through the existing ecr endpoints). Controller, webhook and cainjector are control
# loops -> pinned to the system NG (the add-on schema exposes no startupapicheck key).
resource "aws_eks_addon" "cert_manager" {
  count = local.hyperpod_gw_enabled ? 1 : 0

  cluster_name = module.eks_cluster.cluster_name
  addon_name   = "cert-manager"
  tags         = local.combined_tags

  configuration_values = jsonencode({
    nodeSelector = local.system_node_selector
    tolerations  = [local.system_toleration]
    webhook = {
      nodeSelector = local.system_node_selector
      tolerations  = [local.system_toleration]
    }
    cainjector = {
      nodeSelector = local.system_node_selector
      tolerations  = [local.system_toleration]
    }
  })

  depends_on = [null_resource.cluster_addons, module.node_group]
}

# --- FSx CSI driver (dependency: the operator init check requires CSIDriver fsx.csi.aws.com) ---
#
# Only when enable_fsx is false — with enable_fsx the aws-fsx-csi-driver Helm release
# (platform_fsx.tf) already registers the driver, and two installs would collide. No IAM:
# the driver only needs AWS credentials to provision/describe file systems, which nothing
# does on this path; it exists to satisfy the dependency check.
resource "aws_eks_addon" "hyperpod_fsx_csi_driver" {
  count = local.hyperpod_gw_enabled && !var.enable_fsx ? 1 : 0

  cluster_name = module.eks_cluster.cluster_name
  addon_name   = "aws-fsx-csi-driver"
  tags         = local.combined_tags

  configuration_values = jsonencode({
    controller = {
      nodeSelector = local.system_node_selector
      tolerations  = [local.system_toleration]
    }
    node = {
      tolerations = [{ operator = "Exists" }]
    }
  })

  depends_on = [null_resource.cluster_addons, module.node_group]
}

# --- The HyperPod Inference add-on itself ---
resource "aws_eks_addon" "hyperpod_inference" {
  count = local.hyperpod_gw_enabled ? 1 : 0

  cluster_name  = module.eks_cluster.cluster_name
  addon_name    = "amazon-sagemaker-hyperpod-inference"
  addon_version = var.hyperpod_inference_addon_version
  tags          = local.combined_tags

  configuration_values = jsonencode(merge(
    {
      executionRoleArn       = module.hyperpod_operator_role[0].role_arn
      tlsCertificateS3Bucket = module.hyperpod_tls_bucket[0].bucket_name
      inferenceOperator      = { enabled = true }
      inferenceGateway = {
        enabled        = true
        serviceAccount = { roleArn = module.hyperpod_gateway_controller_role[0].role_arn }
        # Accepted by the v2.1.0-eksbuild.1 schema but NOT rendered onto the controller
        # Deployment (verified live: it schedules anywhere). Kept so placement takes effect
        # once the add-on honors it.
        nodeSelector = local.system_node_selector
        tolerations  = [local.system_toleration]
      }
      keda = { enabled = false }
      alb = {
        enabled        = true
        serviceAccount = { create = true, roleArn = module.hyperpod_alb_controller_role[0].role_arn }
        nodeSelector   = local.system_node_selector
        tolerations    = [local.system_toleration]
      }
    },
    # Optional: only when a HyperPod cluster is attached to this EKS cluster.
    var.hyperpod_cluster_arn == "" ? {} : { hyperpodClusterArn = var.hyperpod_cluster_arn },
  ))

  lifecycle {
    # Both install the Gateway API Inference Extension CRDs (InferencePool): the add-on's
    # gateway controller applies its own copies at start-up, which would fight the
    # helm-owned ones from platform_inference_ext.tf.
    precondition {
      condition     = !var.enable_inference_routing
      error_message = "enable_hyperpod_inference_gateway and enable_inference_routing are mutually exclusive: the HyperPod gateway ships its own InferencePool CRDs."
    }
  }

  depends_on = [
    null_resource.cluster_addons,
    module.node_group,
    aws_eks_addon.cert_manager,
    aws_eks_addon.hyperpod_fsx_csi_driver,
    helm_release.fsx_csi_driver,
    aws_iam_role_policy.hyperpod_alb_controller,
    aws_iam_role_policy.hyperpod_operator_pass_role,
    aws_vpc_endpoint.hyperpod_gateway,
    # The kube-prometheus-stack CRDs (PodMonitor/ServiceMonitor) the gateway emits.
    helm_release.kube_prometheus_stack,
  ]
}

# --- Default InferenceGatewayConfig + Prometheus scraping (charts/hyperpod-gateway) ---
resource "helm_release" "hyperpod_gateway" {
  count = local.hyperpod_gw_enabled ? 1 : 0

  name      = "hyperpod-gateway"
  chart     = "${path.module}/../charts/hyperpod-gateway"
  namespace = kubernetes_namespace_v1.workload.metadata[0].name

  values = [yamlencode({
    gatewayName    = local.hyperpod_gateway_config_name
    schedulerLabel = local.hyperpod_gateway_scheduler_label
    schedulers = [for s in var.hyperpod_inference_gateway_schedulers : {
      name       = s["name"]
      modelName  = s["model_name"]
      targetPort = tonumber(s["target_port"])
    }]
    podDefaults = {
      nodeSelector = local.system_node_selector
      tolerations  = [local.system_toleration]
      # The controller's EPP default is 4 CPU / 8Gi per replica (x2 per scheduler), which
      # cannot fit the m6i.xlarge system NG — pods stay Pending (verified live). The EPP and
      # BBR are light control-plane-ish pods; size them for the system NG.
      resources = {
        requests = { cpu = "250m", memory = "512Mi" }
        limits   = { memory = "2Gi" }
      }
    }
    serviceMonitorLabel = { release = "kube-prometheus-stack" }
    gatewayNamespace    = local.hyperpod_inference_namespace
  })]

  set = [
    { name = "chartContentHash", value = local.chart_hashes["hyperpod-gateway"] },
  ]

  depends_on = [
    aws_eks_addon.hyperpod_inference,
    kubernetes_namespace_v1.workload,
    helm_release.kube_prometheus_stack,
  ]
}
