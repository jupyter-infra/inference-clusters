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

### Installation (with pip)

Recommended: create or activate a python virtual environment.

```bash
pip install "jupyter-deploy[aws,k8s]"
pip install inference-tf-aws-eks-karpenter
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
  **kube-prometheus-stack** (Prometheus + Grafana) for GPU/cluster monitoring
- installs **KEDA** (event/metric-driven autoscaling, incl. scale-to-zero), **KRO**
  (ResourceGraphDefinition starters), **Kueue** (gang scheduling / quota), and
  **LeaderWorkerSet** (multi-node serving)
- installs the **Mountpoint-S3 CSI driver** and creates a **model-store S3 bucket** for
  serving weights, plus a CodeBuild-based onboarder for staging models/images into ECR
- optionally enables a **Gateway API inference-routing** path (InferencePool CRDs + endpoint
  picker)
- embeds a `random_id` postfix in resource names and tags so two deployments can coexist in
  the same account/region
- tags all resources with `Source`, `Template`, `Version`, and `DeploymentId`

## Requirements

| Name | Version |
|---|---|
| terraform | >= 1.5 |
| aws | ~> 5.0 |

## Inputs

All variables are defined in `template/engine/variables.tf`; defaults live in
`template/engine/presets/defaults-all.tfvars`. Run `jd show --variables --list` on a scaffolded
project for the authoritative list with descriptions and recommended values. The main groups:

| Group | Variables (selection) |
|---|---|
| Cluster | `region`, `cluster_name_prefix`, `kubernetes_version`, `custom_tags`, `cluster_log_retention_days` |
| Networking | `enable_nat_gateway` |
| Bootstrap node group | `bootstrap_instance_types`, `bootstrap_desired_size`, `bootstrap_min_size`, `bootstrap_max_size` |
| Access | `admin_role_names`, `admin_user_names` |
| Autoscaling / Karpenter | `karpenter_version`, `cluster_autoscaler_chart_version`, `enable_gpu_p_nodepool`, `gpu_p_capacity_reservation_id` |
| GPU + monitoring | `nvidia_device_plugin_version`, `nvidia_device_plugin_chart_version`, `nvidia_dcgm_exporter_version`, `dcgm_exporter_chart_version`, `kube_prometheus_stack_chart_version`, `grafana_version`, `prometheus_retention`, `prometheus_memory_limit`, `enable_container_insights` |
| Autoscaling operators | `keda_chart_version`, `kro_chart_version` |
| Batch / multi-node | `enable_lws`, `lws_chart_version`, `enable_kueue`, `kueue_chart_version`, `kueue_cluster_queue_name`, `gpu_g_capacity`, `gpu_p_capacity`, `kueue_gpu_lending_limit` |
| Storage / images | `mountpoint_s3_csi_version`, `common_images`, `workload_namespace` |
| Inference routing | `enable_inference_routing` |

## Outputs

| Name | Description |
|---|---|
| `deployment_id` | Unique identifier for this deployment |
| `region` | AWS region where the cluster is deployed |
| `cluster_name` | EKS cluster name |
| `cluster_endpoint` | EKS API server endpoint |
| `cluster_arn` | EKS cluster ARN |
| `cluster_ca_certificate` | Cluster CA certificate (base64) |
| `vpc_id` | VPC ID |
| `kubeconfig_path` | Path to the generated kubeconfig |
| `model_store_bucket` / `model_store_bucket_arn` | S3 bucket for serving weights |
| `models_s3_uri` / `onboarder_input_s3_uri` / `onboarder_output_s3_uri` | Model-onboarding S3 locations |
| `onboarder_codebuild_project` | CodeBuild project that stages models/images |
| `ecr_registry` / `workload_repo_prefix` | ECR registry + workload repo prefix |
| `trusted_upstream_registries` | Registries the onboarder may pull from |
| `*_ecr_repository` | Vendored-image ECR repos (KEDA, Grafana, DCGM, device-plugin) |
| `vendored_image_tag` | Tag applied to vendored images |
| `starter_rgd_names` | KRO ResourceGraphDefinition starter names |
| `*_namespace` | Namespaces for the installed platform components |

Run `jd show --outputs --list` for the complete list.

## License

This project is licensed under the [MIT License](LICENSE).
