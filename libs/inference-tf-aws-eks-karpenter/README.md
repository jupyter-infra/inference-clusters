# Inference — AWS EKS Karpenter template

An open-source [jupyter-deploy](https://github.com/jupyter-infra/jupyter-deploy) Terraform
template that provisions an **AWS EKS cluster** with [Karpenter](https://karpenter.sh) for node
autoscaling over **self-managed nodes**, intended as the foundation that inference workloads are
layered onto. It ships the platform components a GPU inference stack needs, and supports serving Generative AI models within an internet-free VPC.

- infrastructure-as-code engine: `terraform`
- cloud provider: `aws`
- node autoscaling: Karpenter (self-managed nodes)

## Prerequisites

- an AWS account with permissions to create EKS, VPC, IAM, and ECR resources
- [terraform](https://developer.hashicorp.com/terraform/tutorials/aws-get-started/install-cli)
- the [aws-cli](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [helm](https://helm.sh/docs/intro/install/)

## Usage

This template is meant to be used with the
[jupyter-deploy](https://github.com/jupyter-infra/jupyter-deploy) CLI.

### Installation

Recommended: use [uv](https://docs.astral.sh/uv/) to create a virtual environment.

```bash
# prepare your virtual environment
uv init . --bare
uv venv
source .venv/bin/activate

# install the dependencies
uv add "jupyter-deploy[aws,k8s]" inference-tf-aws-eks-karpenter
```

Or with pip, into an active virtual environment:

```bash
pip install "jupyter-deploy[aws,k8s]" inference-tf-aws-eks-karpenter
```

### Project setup

```bash
mkdir my-inference-cluster
cd my-inference-cluster

jd init . -E terraform -P aws -I eks -T karpenter
```

### Configure and create the cluster

```bash
jd config
jd up
```

A full EKS + Karpenter deploy takes ~20–30 minutes. `jd up` backs up the project (files +
terraform state) to a remote S3 store after every run, even on failure.

### Access the cluster

```bash
# fetch a kubeconfig for the cluster
jd cluster login

# then use kubectl / helm as usual
kubectl get nodes
```

### Inspect outputs

```bash
# list all outputs
jd show --outputs --list

# get a specific value
jd show -o cluster_name --text
jd show -o model_store_bucket --text
```

### Take down the cluster

This removes all resources associated with the project in your AWS account.

```bash
jd down
```

## Details

This project:
- provisions a VPC (public/private subnets; optional NAT gateway) and an EKS cluster with a
  small **bootstrap** managed node group that hosts the platform components
- installs **Karpenter** and node pools so GPU/CPU capacity is provisioned on demand over
  **self-managed nodes**, with an optional dedicated GPU (`p`-family) node pool
- installs the **NVIDIA device plugin** and **DCGM exporter**, with a
  **kube-prometheus-stack** (Prometheus + Alertmanager) for GPU/cluster monitoring, with
  **Grafana** dashboards as an opt-in (`enable_grafana`; AGPL-3.0, see
  [External Dependencies](#external-dependencies))
- installs **KEDA** (event/metric-driven autoscaling, incl. scale-to-zero), **KRO**
  (ResourceGraphDefinition starters), **Kueue** (gang scheduling / quota), and
  **LeaderWorkerSet** (multi-node serving)
- installs the **Mountpoint-S3 CSI driver** and creates a **model-store S3 bucket** for
  serving weights, plus a CodeBuild-based onboarder for staging models/images into ECR
- optionally enables **FSx for Lustre** (PERSISTENT_2 SSD + LZ4) as an RWX shared file
  system fronting the same S3 model store via a Data Repository Association, exposed to
  workloads as a static PV/PVC through the `aws-fsx-csi-driver`
- optionally enables a **Gateway API inference-routing** path (InferencePool CRDs + endpoint
  picker)
- optionally installs the **SageMaker HyperPod Inference Gateway** (`enable_hyperpod_inference_gateway`):
  the `amazon-sagemaker-hyperpod-inference` EKS add-on (operator + gateway) with a default
  `InferenceGatewayConfig` — see [SageMaker HyperPod Inference Gateway](#sagemaker-hyperpod-inference-gateway-opt-in)
- embeds a `random_id` postfix in resource names and tags so two deployments can coexist in
  the same account/region
- tags all resources with `Source`, `Template`, `Version`, and `DeploymentId`

## SageMaker HyperPod Inference Gateway (opt-in)

Set `enable_hyperpod_inference_gateway = true` to install the
[SageMaker HyperPod Inference Gateway](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-model-deployment-inference-gateway.html),
an LLM-aware routing layer: **Envoy Gateway** -> **Body-Based Router** (reads the OpenAI body
`model`) -> per-model **llm-d Endpoint Picker** (scores pods on queue depth, KV-cache
utilization and prefix-cache affinity).

What the template provisions (all gated on the variable, see `engine/platform_hyperpod_gateway.tf`):
- the `amazon-sagemaker-hyperpod-inference` EKS add-on (`hyperpod_inference_addon_version`,
  default `v2.1.0-eksbuild.1`; the gateway needs `v2.0.0-eksbuild.2`+) with
  `inferenceOperator.enabled` and `inferenceGateway.enabled`. The add-on's bundled **KEDA** is
  turned off (the template already runs KEDA); its bundled **AWS Load Balancer Controller** stays
  on, because the operator requires its CRDs (see prerequisites below);
- the `cert-manager` EKS add-on, and the `aws-fsx-csi-driver` EKS add-on when `enable_fsx` is
  false (with `enable_fsx` the template's FSx Helm release already registers the driver);
- an **IAM OIDC provider** and three **IRSA** roles — the add-on only supports IRSA (its
  `podIdentityConfiguration` is empty), unlike the rest of the template, which uses Pod Identity:
  the operator execution role (`AmazonSageMakerHyperPodInferenceAccess`), the gateway controller
  role (`AmazonSageMakerHyperPodInferenceGatewayAccess`) and the bundled ALB controller role (the
  AWS Load Balancer Controller policy the add-on documents, `engine/policies/`);
- a `hyperpod-tls-<postfix>-*` S3 bucket for `tlsCertificateS3Bucket` (the managed policy only
  covers `hyperpod-tls*` buckets);
- `sagemaker.api` and `acm` interface endpoints (the base VPC already has sts/eks/ecr/elb);
- a default `InferenceGatewayConfig` named `inference-gateway` in the workload namespace
  (`charts/hyperpod-gateway`), with `bbr.enabled`, `tls: {}` and one scheduler per
  `hyperpod_inference_gateway_schedulers` entry, plus PodMonitors so the in-cluster Prometheus
  scrapes the gateway's routing metrics.

**Add-on prerequisites (verified live).** The operator pod's `check-csi-drivers` init container
refuses to start (the add-on then never leaves `CREATING`) unless the cluster already has:
the `s3.csi.aws.com` **and** `fsx.csi.aws.com` CSIDrivers, the AWS Load Balancer Controller CRDs
(`ingressclassparams.elbv2.k8s.aws`, `targetgroupbindings.elbv2.k8s.aws`), the KEDA CRDs
(`scaledobjects.keda.sh`, `scaledjobs.keda.sh`) and the cert-manager CRDs. The template satisfies
all of them; a hand-rolled install must too.

**TLS is mandatory.** The `InferenceGatewayConfig` CRD requires `spec.tls` — there is no
HTTP-only mode. The default `tls: {}` auto-issues a self-signed certificate with cert-manager and
imports it into ACM (gateway-controller IRSA role + `acm` endpoint); set the chart's
`tls.acmArn` to bring your own certificate. Clients inside the VPC must trust that certificate
(or skip verification for the self-signed default).

**Endpoint and readiness (verified live).** The gateway controller creates the Gateway
`inference-gateway` in `hyperpod-inference-system`. Its Envoy Service (find it with the
`hyperpod_inference_gateway_service_selector` output; HTTP :80 in-cluster) gets an **internal
NLB**, and an **internal ALB** Ingress `inference-gateway-alb` serves HTTPS 443 with the
auto-issued ACM certificate. Both are created by the add-on's ALB controller, not by Terraform,
and are removed when the `InferenceGatewayConfig` is deleted (the chart uninstalls before the
add-on on destroy). The IGC reports `Accepted`, `GatewayProgrammed` and `BBRReady` once the
platform side is up; `Ready` stays `False` (`BackendsNotReady`) until at least one labeled model
pod is serving.

**Attaching a model.** A scheduler `{name, model_name, target_port}` routes requests whose body
`model` equals `model_name` to the pods labeled `inference/gateway-scheduler: <name>` in the
workload namespace. So a model Deployment (or the KRO starter `InferenceDeployment`, via its
`gatewayScheduler` field) joins the gateway by carrying that label and serving under exactly
`model_name` (vLLM `--served-model-name`). Add more models by adding schedulers.

**Constraints.** Mutually exclusive with `enable_inference_routing` (the add-on installs its own
InferencePool CRDs). `hyperpod_inference_gateway_schedulers` is a `list(map(string))` (keys
`name`, `model_name`, `target_port`) rather than a `list(object(...))`, because `jd` only parses
string / number / bool / `list(string)` / `map(string)` / `list(map(string))` variable types.

**Model-server version.** The EPP scrapes each model pod's metrics to score it (queue depth,
KV-cache utilization) and requires **vLLM >= 0.9.2** (or SGLang >= 0.3.5.post1 with
`--enable-metrics`). Older servers are still routed to, but the EPP cannot read their metrics:
`inference_pool_ready_pods` reads 0 and `llm_d_epp_datalayer_extract_errors_total` keeps
climbing (observed live with the vLLM v0.6.6 e2e fixture). No request authentication is configured by default
(`spec.auth.jwt` is the knob) — access is bounded by the VPC.

**Does it need a HyperPod cluster?** The **operator** half does: at start-up it calls
SageMaker `UpdateClusterInference` (EnableClusterInference) on the attached HyperPod cluster —
discovered even when `hyperpod_cluster_arn` is empty — passing its own execution role (hence the
template's least-privilege `iam:PassRole` grant), so on an EKS cluster with no HyperPod cluster
the operator has nothing to enable. The **gateway** half does not depend on HyperPod: the
gateway controller, its `inference-gateway` GatewayClass, Envoy, BBR and the EPPs are plain
Kubernetes objects, and the add-on schema/release notes allow `inferenceOperator.enabled=false`
with only the gateway on. AWS nevertheless publishes the add-on for `computeTypes: ["hyperpod"]`
and documents an EKS-orchestrated HyperPod cluster as a prerequisite, so a gateway-only install
on plain EKS is outside AWS's supported configuration (not exercised by this template).

## Requirements

| Name | Version |
|---|---|
| terraform | >= 1.5 |
| aws | ~> 5.0 |

## Inputs

All variables are defined in `template/engine/variables.tf`; defaults live in
`template/engine/presets/defaults-all.tfvars`. Run `jd show --variables --list` on a scaffolded
project for descriptions and recommended values. Grouped by concern:

| Group | Variables |
|---|---|
| Cluster | `region`, `cluster_name_prefix`, `kubernetes_version`, `custom_tags`, `cluster_log_retention_days` |
| Networking | `enable_nat_gateway` |
| Bootstrap node group | `bootstrap_instance_types`, `bootstrap_desired_size`, `bootstrap_min_size`, `bootstrap_max_size` |
| Access | `admin_role_names`, `admin_user_names` |
| Autoscaling / Karpenter | `karpenter_version`, `cluster_autoscaler_chart_version`, `enable_gpu_p_nodepool`, `gpu_p_capacity_reservation_id`, `gpu_parallel_image_pull`, `cpu_capacity`, `memory_capacity` |
| GPU + monitoring | `nvidia_device_plugin_version`, `nvidia_device_plugin_chart_version`, `nvidia_dcgm_exporter_version`, `dcgm_exporter_chart_version`, `kube_prometheus_stack_chart_version`, `enable_grafana`, `grafana_version`, `prometheus_retention`, `prometheus_memory_limit`, `enable_container_insights`, `metrics_server_chart_version` |
| Autoscaling operators | `keda_chart_version`, `kro_chart_version` |
| Batch / multi-node | `enable_lws`, `lws_chart_version`, `enable_kueue`, `kueue_chart_version`, `kueue_cluster_queue_name`, `gpu_g_capacity`, `gpu_p_capacity`, `kueue_gpu_lending_limit`, `enable_efa`, `efa_device_plugin_chart_version`, `efa_device_plugin_image_tag` |
| Storage / images | `mountpoint_s3_csi_version`, `common_images`, `workload_namespace` |
| FSx for Lustre (opt-in) | `enable_fsx`, `fsx_storage_capacity_gib`, `fsx_per_unit_storage_throughput`, `fsx_imported_file_chunk_size_mib`, `fsx_kms_key_arn`, `fsx_csi_driver_chart_version` |
| Inference routing | `enable_inference_routing` |
| HyperPod Inference Gateway (opt-in) | `enable_hyperpod_inference_gateway`, `hyperpod_inference_addon_version`, `hyperpod_cluster_arn`, `hyperpod_inference_gateway_schedulers` |

## Outputs

| Name | Description |
|---|---|
| `deployment_id` | Unique identifier for this deployment |
| `region` | AWS region where the cluster is deployed |
| `cluster_name` | EKS cluster name |
| `cluster_endpoint` | EKS API server endpoint |
| `cluster_arn` | EKS cluster ARN |
| `platform_mng_names` | Names of the EKS managed node groups (backs `jd pool status`) |
| `cluster_ca_certificate` | Cluster CA certificate (base64) |
| `vpc_id` | VPC ID |
| `kubeconfig_path` | Path to the generated kubeconfig |
| `model_store_bucket` / `model_store_bucket_arn` | S3 bucket for serving weights |
| `batch_intake_bucket` / `batch_intake_bucket_arn` | S3 bucket batch-inference requests flow into |
| `batch_output_bucket` / `batch_output_bucket_arn` | S3 bucket batch-inference results and metrics land in |
| `batch_inference_service_account_name` | Service account with batch S3 access via Pod Identity |
| `batch_storage_config_map_name` | ConfigMap of batch bucket names and AWS Region |
| `models_s3_uri` / `onboarder_input_s3_uri` / `onboarder_output_s3_uri` | Model-onboarding S3 locations |
| `onboarder_codebuild_project` | CodeBuild project that stages models/images |
| `image_build_codebuild_project` / `image_build_input_s3_uri` | CodeBuild project that builds a consumer-uploaded source dir into a workload/* ECR image, + the S3 prefix to upload source to |
| `ecr_registry` / `workload_repo_prefix` | ECR registry + workload repo prefix |
| `trusted_upstream_registries` | Registries the onboarder may pull from |
| `*_ecr_repository` | Vendored-image ECR repos (KEDA, DCGM, device-plugin; Grafana only when it is enabled) |
| `vendored_image_tag` | Tag applied to vendored images |
| `starter_rgd_names` | KRO ResourceGraphDefinition starter names |
| `*_namespace` | Namespaces for the installed platform components |
| `fsx_enabled` | Whether the FSx for Lustre file system is provisioned |
| `fsx_file_system_id` / `fsx_file_system_arn` | ID + ARN of the shared FSx for Lustre file system (empty when disabled) |
| `fsx_dns_name` / `fsx_mount_name` | DNS + Lustre mount name — the two pieces of a CSI volumeHandle (empty when disabled) |
| `fsx_availability_zone` | AZ the FSx file system lives in — pin FSx-consumer pods here (empty when disabled) |
| `fsx_data_repository_path` | S3 URI the FSx `/models` mount is linked to via the DRA (empty when disabled) |
| `hyperpod_inference_gateway_enabled` | Whether the HyperPod Inference add-on + gateway are installed |
| `hyperpod_inference_gateway_config_name` | Name of the default InferenceGatewayConfig (workload namespace; empty when disabled) |
| `hyperpod_inference_gateway_scheduler_label` | Pod label key that attaches a model Deployment to a gateway scheduler |
| `hyperpod_inference_gateway_models` | Model names (the OpenAI request model field) the default gateway routes (empty when disabled) |
| `hyperpod_inference_gateway_service_selector` | Label selector of the gateway's Envoy Service in the add-on namespace — the in-VPC endpoint (http://SERVICE.NAMESPACE.svc:80) |
| `hyperpod_inference_tls_bucket` / `hyperpod_inference_execution_role_arn` | The add-on's TLS-certificate bucket + IRSA execution role (empty when disabled) |

Run `jd show --outputs --list` for the complete list.

## External Dependencies

This package depends on and may incorporate or retrieve a number of third-party
software packages (such as open source packages) at install-time or build-time
or run-time ("External Dependencies"). The External Dependencies are subject to
license terms that you must accept in order to use this package. If you do not
accept all of the applicable license terms, you should not use this package. We
recommend that you consult your company’s open source approval policy before
proceeding.

Provided below is a list of External Dependencies and the applicable license
identification as indicated by the documentation associated with the External
Dependencies as of Amazon's most recent review.

THIS INFORMATION IS PROVIDED FOR CONVENIENCE ONLY. AMAZON DOES NOT PROMISE THAT
THE LIST OR THE APPLICABLE TERMS AND CONDITIONS ARE COMPLETE, ACCURATE, OR
UP-TO-DATE, AND AMAZON WILL HAVE NO LIABILITY FOR ANY INACCURACIES. YOU SHOULD
CONSULT THE DOWNLOAD SITES FOR THE EXTERNAL DEPENDENCIES FOR THE MOST COMPLETE
AND UP-TO-DATE LICENSING INFORMATION.

YOUR USE OF THE EXTERNAL DEPENDENCIES IS AT YOUR SOLE RISK. IN NO EVENT WILL
AMAZON BE LIABLE FOR ANY DAMAGES, INCLUDING WITHOUT LIMITATION ANY DIRECT,
INDIRECT, CONSEQUENTIAL, SPECIAL, INCIDENTAL, OR PUNITIVE DAMAGES (INCLUDING
FOR ANY LOSS OF GOODWILL, BUSINESS INTERRUPTION, LOST PROFITS OR DATA, OR
COMPUTER FAILURE OR MALFUNCTION) ARISING FROM OR RELATING TO THE EXTERNAL
DEPENDENCIES, HOWEVER CAUSED AND REGARDLESS OF THE THEORY OF LIABILITY, EVEN
IF AMAZON HAS BEEN ADVISED OF THE POSSIBILITY OF SUCH DAMAGES. THESE LIMITATIONS
AND DISCLAIMERS APPLY EXCEPT TO THE EXTENT PROHIBITED BY APPLICABLE LAW.

- Grafana (AGPL-3.0) — https://github.com/grafana/grafana — only deployed when
  `enable_grafana = true` (off by default).

## License

This project is licensed under the [MIT License](LICENSE).
