# FSx research notes

Deep-dive research notes on **Amazon FSx** (with a strong bias toward **FSx for
Lustre** on **EKS**) for the `inference-clusters` monorepo. Each note in this
directory is a self-contained study of one facet of the stack; this README is a
map into them.

## How this was produced

These notes were produced by a set of **parallel research agents**, one per
topic. Each agent was given a single narrow scope (architecture, S3 data
repositories, the EKS CSI driver, ML patterns, networking/security, cost,
Terraform, gotchas) and produced one Markdown file end-to-end with citations.
The write-ups are meant to be read as first-party research (with links to AWS
docs and upstream sources), not as normative repo documentation — treat them
as inputs to future template design work.

## Reading order

If you are new to this stack, read in this order. Each note assumes the
concepts introduced in the earlier ones.

1. [`overview.md`](./overview.md) — the FSx family (Lustre, OpenZFS, ONTAP,
   Windows), a decision matrix, and the case for FSx for Lustre as the default
   for ML/inference on EKS. Start here if you have not already picked a flavour.
2. [`lustre-architecture.md`](./lustre-architecture.md) — mental model of what
   Lustre is (MDS/OSS/LNet/LOV, striping), what FSx actually manages for you,
   and what a read/write from an EKS pod really does.
3. [`lustre-s3-drs.md`](./lustre-s3-drs.md) — how FSx for Lustre and S3 stay in
   sync via Data Repository Associations, lazy loading, and explicit hydration.
   This is the ML-weight story.
4. [`eks-csi-driver.md`](./eks-csi-driver.md) — how you actually mount an FSx
   file system from a pod: `aws-fsx-csi-driver`, `StorageClass`, static vs
   dynamic PV/PVC, IRSA vs Pod Identity.
5. [`ml-inference-patterns.md`](./ml-inference-patterns.md) — patterns on top of
   the primitives above: sizing, striping, warm-pool hydration with Karpenter,
   and where FSx wins vs Mountpoint-S3 / S3 Express / EFS / EBS / NVMe.
6. [`networking-security.md`](./networking-security.md) — subnet placement,
   security groups (988 + 1018-1023), MTU, DNS, KMS, service-linked roles,
   Pod Identity — the details you need before Terraforming.
7. [`cost-perf-benchmarks.md`](./cost-perf-benchmarks.md) — cost model,
   throughput math, LZ4 compression, worked sizing examples for Llama-3
   inference/training, and $/GB comparisons across storage services.
8. [`terraform-eks-integration.md`](./terraform-eks-integration.md) — a
   concrete Terraform + Helm + PV wiring targeted at this repo's conventions
   (`random_id.postfix`, `DeploymentId` tags, `charts/…` layout, Karpenter
   `EC2NodeClass`).
9. [`gotchas-limits.md`](./gotchas-limits.md) — the operational failure modes
   (mount-name churn on re-creation, kernel/AMI compatibility, evictions,
   6-hour cool-downs, DRA limits, `flock`). Read this before you deploy.

## Table of contents

| File | One-line summary |
| --- | --- |
| [`overview.md`](./overview.md) | Entry-point overview of the Amazon FSx family (Lustre, OpenZFS, ONTAP, Windows) with per-flavor deep dives, a decision matrix, and the case for FSx for Lustre as the default for ML/inference on EKS. |
| [`lustre-architecture.md`](./lustre-architecture.md) | Deep dive on FSx for Lustre internals — MDS/OSS/LNet/LOV/striping, SCRATCH vs PERSISTENT_1/2 deployment types with per-TiB throughput and per-OSS sizing, KMS/in-transit encryption, VPC/ENI mount model, backup semantics, and what a read/write actually does from an EKS pod through the `aws-fsx-csi-driver`. |
| [`lustre-s3-drs.md`](./lustre-s3-drs.md) | How FSx for Lustre data repository associations link Lustre directories to S3 prefixes, with auto-import/auto-export event policies, HSM-driven lazy loading and preloading via `lfs hsm_restore`/`hsm_action`, POSIX-to-S3 metadata mapping, and the ML pattern of keeping weights and datasets as source-of-truth in S3 while hydrating on demand for high-throughput GPU reads. |
| [`eks-csi-driver.md`](./eks-csi-driver.md) | Deep dive on the `aws-fsx-csi-driver` on EKS: install paths (EKS add-on, Helm, Kustomize), IRSA vs Pod Identity, full StorageClass parameter reference, static vs dynamic PV/PVC/Pod YAML, mount options (`flock`), reclaim-policy pitfalls, and multi-AZ scheduling. |
| [`ml-inference-patterns.md`](./ml-inference-patterns.md) | End-to-end guide to FSx for Lustre on EKS for LLM/diffusion inference and training: sizing, striping, client tuning, hydration/warm-pool patterns with Karpenter, and tradeoffs vs S3 Mountpoint, S3 Express, EFS, EBS, and NVMe instance store. |
| [`networking-security.md`](./networking-security.md) | Production-grade networking and security guide for FSx for Lustre on EKS: single-AZ subnet placement, TCP 988/1018-1023 security groups (EFA vs non-EFA), MTU 9001, DNS resolution, PrivateLink for control plane only, service-linked roles, KMS CMK policy with `kms:ViaService`, Nitro-only in-transit encryption by deployment type, and Pod Identity vs IRSA for the CSI driver with a hardened Terraform/Helm/manifest example. |
| [`cost-perf-benchmarks.md`](./cost-perf-benchmarks.md) | FSx for Lustre cost model, throughput math (TiB x MB/s/TiB), IOPS/metadata characteristics, per-instance saturation, LZ4 compression impact, sizing worked examples for Llama-3 inference/training/checkpoints, and $/GB comparison vs S3/EFS/EBS/NVMe. |
| [`terraform-eks-integration.md`](./terraform-eks-integration.md) | Practical Terraform guide for provisioning FSx for Lustre (PERSISTENT_2 + DRA) alongside a Karpenter-driven EKS cluster: file-system arguments, security-group pair, AZ-pinned NodePool topology, service-linked role, FSx CSI driver via Helm + Pod Identity, and a static PV/PVC chart wiring — matched to this repo's `random_id` + `DeploymentId` + `charts/storage` conventions. |
| [`gotchas-limits.md`](./gotchas-limits.md) | Operational gotchas, quotas, and failure modes for FSx for Lustre on EKS — DRA limits, mount-name churn, `flock`, kernel/AMI matrix (incl. Bottlerocket gap), scaling cool-downs, evictions, and CSI-driver failure recovery with a runbook. |

## Repo-specific next steps

Concrete follow-ups for the `inference-clusters` monorepo (specifically the
`libs/inference-tf-aws-eks-karpenter` template), grounded in what the notes
above actually found:

- **Add an FSx for Lustre module alongside the existing S3-direct and
  Mountpoint-S3 paths in `platform_storage.tf`.** The Terraform note calls out
  that this repo already exposes two weight-serving paths (S3-direct and
  S3-mount via the Mountpoint-for-S3 CSI driver) and positions FSx for Lustre
  as the third — RWX, POSIX, sub-ms metadata, sized by throughput tier — for
  workloads whose S3 GET fan-out is the bottleneck. Follow the repo
  conventions (`${local.resource_name_prefix}-…`, `local.combined_tags` with
  `DeploymentId`, defaults in `engine/presets/defaults-all.tfvars`, a
  first-party `charts/fsx` chart).
- **Pin an FSx-consuming Karpenter NodePool to a single AZ.** The
  networking/security note and the Terraform note are emphatic that FSx for
  Lustre (except PERSISTENT_2 Intelligent-Tiering) is single-AZ, and any
  Karpenter `EC2NodeClass` that provisions nodes intended to mount the FS
  must restrict `subnetSelectorTerms` to the same AZ. Encode this as a
  dedicated `NodePool` (e.g. `gpu-fsx-az-a`) rather than trying to make the
  default pool AZ-aware.
- **Wire the `aws-fsx-csi-driver` controller SA via EKS Pod Identity, not
  IRSA.** The CSI-driver and networking notes both recommend Pod Identity for
  modern clusters, and the Terraform note observes this repo already uses
  Pod Identity for the EBS and S3 CSI drivers in `eks_addons.tf`. Reuse that
  pattern; attach `AmazonFSxFullAccess` (or a scoped-down equivalent that
  keeps `iam:CreateServiceLinkedRole` for `fsx.amazonaws.com` and the S3
  reads DRA needs).
- **Bake an S3-DRA-backed "hydrate before scale-out" pattern into the
  template.** The ML-patterns and DRA notes recommend an S3 DRA with
  auto-import on but auto-export deliberately off, plus a one-shot
  `model-puller` Job that `lfs hsm_restore`s the model tree so GPU nodes
  never take a lazy-load hit at first-token time. This composes naturally
  with the shared benchmark Pod Identity story already in the repo (see
  commit `1f680e3`).
- **Default a new FSx variable set to PERSISTENT_2 + 250 MB/s/TiB + LZ4.**
  The Terraform note nominates PERSISTENT_2 with `per_unit_storage_throughput
  = 250` as a sensible inference default (DRA-capable, room to burst), and
  the cost/perf note calls LZ4 compression a free win for AI/ML because it
  reduces stored bytes and can push effective disk throughput toward the
  network cap. Both should ship as defaults in `presets/defaults-all.tfvars`,
  with the throughput tier and total capacity exposed as first-class
  `variables.tf` knobs.
- **Add pre-flight checks for the two things that will bite operators.** The
  gotchas note names them concretely: (a) kernel/AMI matrix — Bottlerocket
  historically shipped without the Lustre client kmod and AL2023 needs
  `>=6.1.79-99.167.amzn2023` for Lustre 2.15, so a template that offers FSx
  must gate on AMI family/version; and (b) `flock` — always mount with
  `-o flock,relatime,_netdev` because torch/lightning/sqlite/git/apt/pip all
  break subtly without it. Bake the mount option into the `StorageClass` and
  document the AMI matrix in the template's `AGENT.md.template`.
- **Do not assume a Lustre PV survives a re-create.** The gotchas note warns
  that DNS name, IP, and the 8-character `mountname` change on file-system
  re-creation and many restores, breaking static PVs and `/etc/fstab`
  entries. If the template supports `terraform destroy` + re-apply
  cycles (as the repo generally does via `random_id.postfix`), the PV/PVC
  chart must be regenerated from live outputs — not templated once at
  `jd init`.
