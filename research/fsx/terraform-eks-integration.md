---
title: "Terraform + EKS integration — provisioning FSx for Lustre alongside a Karpenter cluster"
audience: infra engineers, ML platform engineers
scope: FSx for Lustre on EKS, Karpenter-scaled self-managed nodes, jupyter-deploy monorepo shape
---

# Terraform + EKS integration — provisioning FSx for Lustre alongside a Karpenter cluster

## TL;DR

- Provision the file system with [`aws_fsx_lustre_file_system`](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/fsx_lustre_file_system) using `deployment_type = "PERSISTENT_2"` and `per_unit_storage_throughput = 250` as a sensible inference default; that combination unlocks [`aws_fsx_data_repository_association`](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/fsx_data_repository_association) for S3 linkage, which is the FSx Lustre S3 story on any post-2021 file system.
- FSx for Lustre is **AZ-local**: `subnet_ids` accepts exactly one subnet, so pick a subnet in the same AZ Karpenter is allowed to launch GPU nodes into (or run a file system per AZ).
- Ports **TCP 988** and **1018–1023** are required inbound on the FSx SG (and outbound on the client SG) for Lustre traffic; the simplest wiring is a self-referencing SG shared by nodes and file system per [FSx docs](https://docs.aws.amazon.com/fsx/latest/LustreGuide/limit-access-security-groups.html).
- The [`AWSServiceRoleForAmazonFSx`](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-service-linked-roles.html) service-linked role is auto-created on the first `CreateFileSystem` call, but pre-creating it with `aws_iam_service_linked_role` avoids a race on first-apply in fresh accounts. A second SLR (`AWSServiceRoleForFSxS3Access_<fs-id>`) is created per file system that links to S3.
- Expose `fs_id`, `dns_name`, and `mount_name` as Terraform outputs — those three attributes are exactly what the [FSx CSI driver](https://github.com/kubernetes-sigs/aws-fsx-csi-driver) needs to bind a static PV (`volumeHandle = <fs_id>::<mount_name>`, `volumeAttributes.{dnsname,mountname}`).
- Install the CSI driver via the [`aws-fsx-csi-driver` Helm chart](https://kubernetes-sigs.github.io/aws-fsx-csi-driver) and grant its controller service account `AmazonFSxFullAccess` through EKS Pod Identity — the same pattern this repo already uses for the EBS/S3 CSI drivers (see `libs/inference-tf-aws-eks-karpenter/inference_tf_aws_eks_karpenter/template/engine/eks_addons.tf`).
- Follow the repo conventions: name every resource under `${local.resource_name_prefix}-...` (which already embeds `random_id.postfix.hex`), tag every resource with `local.combined_tags` (which carries `DeploymentId = random_id.postfix.hex`), keep variable defaults in `engine/presets/defaults-all.tfvars`, and expose a first-party chart under `charts/fsx` for the StorageClass + PV.

---

## 1. Where FSx for Lustre fits in an inference cluster

The repo already ships two weight-serving paths (see `platform_storage.tf` in the EKS Karpenter template):

1. **S3-direct** — engines stream weights from S3 using the node role or a Pod Identity role.
2. **S3-mount** — the [Mountpoint-for-S3 CSI driver](https://github.com/awslabs/mountpoint-s3-csi-driver) presents `s3://bucket/models` as a read-only POSIX path.

FSx for Lustre is the third path, and it targets a different regime:

- **Sub-ms metadata + hundreds of GB/s aggregate throughput**. Mountpoint-S3 goes to real S3 for every miss — great for cold starts of many small models but limited by S3 request rates and per-object semantics. Lustre serves from SSD (PERSISTENT_2 SSD) or intelligent tiering with real POSIX (locks, mmap, `unlink`, `rename`).
- **Read-write, shared, cluster-wide**. All Lustre client access modes are effectively `ReadWriteMany`. This makes FSx the natural home for training checkpoint scratch, LoRA adapters written during fine-tuning, or a shared dataset cache used across an inference DP mesh.
- **Auto-tiering to S3 via Data Repository Associations (DRAs)**. Lustre and S3 stay in sync in both directions with the `AutoImportPolicy`/`AutoExportPolicy` events (`NEW`, `CHANGED`, `DELETED`). This lets the same S3 bucket that the onboarder writes weights into serve as the source of truth, while Lustre is the hot cache.

A useful rule of thumb from the [AWS ML blog series on FSx for Lustre + EKS](https://aws.amazon.com/blogs/storage/optimize-training-time-with-amazon-fsx-for-lustre-and-amazon-eks/):

| Concern                                       | S3-direct | Mountpoint-S3 | FSx Lustre    |
|-----------------------------------------------|-----------|---------------|---------------|
| Cold-start throughput per pod                 | High      | High          | Very high     |
| Aggregate throughput across a fleet           | Bounded by S3 prefix / bucket 10s of GB/s | Same as S3 direct + FS overhead | 125–1000 MB/s/TiB linearly, up to hundreds of GB/s |
| Write support                                 | Yes (SDK) | No (read-only mount) | Yes           |
| POSIX semantics (mmap, locks, rename, holes)  | N/A       | Partial       | Full          |
| Small-file / metadata-heavy workloads         | Poor      | Poor          | Good          |
| Hourly cost floor                             | $0        | $0            | Non-trivial (per-TB per-hour) |
| S3 sync (import/export)                       | Native    | Native (it *is* S3) | DRA           |

For an inference-only cluster that only ever reads write-once weights, Mountpoint-S3 is fine. For anything that needs writes on a hot path (KV-cache offload, RAG index rebuilds, LoRA hot-swap staging, training checkpoints), FSx Lustre is the answer.

---

## 2. `aws_fsx_lustre_file_system` — argument-by-argument

The canonical reference is the Terraform Registry page: [`aws_fsx_lustre_file_system`](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/fsx_lustre_file_system). Below is what every important argument actually controls, and what values make sense in an inference cluster.

### 2.1 Deployment type — `deployment_type`

Allowed values: `SCRATCH_1`, `SCRATCH_2`, `PERSISTENT_1`, `PERSISTENT_2`. `PERSISTENT_2` is what you want.

- **SCRATCH_1 / SCRATCH_2** — ephemeral, no automatic replication of data across servers, single-AZ. Cheapest per GiB. Data on a failed server is lost. Fine for pre-training scratch, unacceptable for anything you cannot re-derive from S3.
- **PERSISTENT_1** — durable (data replicated across servers within one AZ), supports SSD and HDD, up to 200 MB/s/TiB throughput. Older generation.
- **PERSISTENT_2** — durable, SSD or [Intelligent Tiering](https://aws.amazon.com/blogs/aws/new-amazon-fsx-for-lustre-intelligent-tiering-a-fully-elastic-file-storage-that-costs-up-to-96-less-for-infrequently-accessed-data/), 125–1000 MB/s/TiB. **Required for `aws_fsx_data_repository_association`.** This is the modern default.

Choose `PERSISTENT_2` unless you have a specific ephemeral use case; DRA gating alone is dispositive.

### 2.2 Storage capacity and throughput

`storage_capacity` (in GiB) and `per_unit_storage_throughput` (in MB/s per TiB) together set aggregate throughput. Constraints:

- `PERSISTENT_2 SSD`: `storage_capacity` in 1200-GiB increments (1200, 2400, 4800, 7200, …). `per_unit_storage_throughput` ∈ {125, 250, 500, 1000}.
- `PERSISTENT_1 SSD`: capacity multiples of 2400 GiB after 1200. `per_unit_storage_throughput` ∈ {50, 100, 200}.
- `PERSISTENT_1 HDD`: capacity multiples of 6000 GiB (throughput 12) or 1800 GiB (throughput 40). Requires `drive_cache_type`.
- `SCRATCH_2`: `storage_capacity` in 1200-GiB increments; no `per_unit_storage_throughput`.

Recommended default for a mid-size inference cluster: **`storage_capacity = 4800`, `per_unit_storage_throughput = 250`** → 4800 GiB × 250 MB/s/TiB / 1024 ≈ **1.17 GB/s aggregate**. That is enough headroom for a fleet of a dozen GPU nodes each streaming 300–500 MB/s. Bump `per_unit_storage_throughput` to 500 (2.34 GB/s) or 1000 (4.68 GB/s) for P4/P5-heavy fleets or for training checkpoints.

### 2.3 Networking — `subnet_ids`, `security_group_ids`

- `subnet_ids` — required, **exactly one** subnet on Lustre (unlike, say, EFS). FSx creates ENIs in that subnet and only clients that can route to that AZ can mount at line rate. Cross-AZ mounts work but incur inter-AZ transfer.
- `security_group_ids` — up to 5. The SG must permit `TCP 988` and `TCP 1018–1023` from every client SG (and vice versa), per [FSx security group docs](https://docs.aws.amazon.com/fsx/latest/LustreGuide/limit-access-security-groups.html). The cleanest wiring is one SG that both the file system and the node SG allow via self-reference on the required ports.

Because Karpenter chooses subnets by tag (`karpenter.sh/discovery = <cluster>` in this repo — see `modules/vpc/main.tf`), you must reason about **which** subnet FSx is in relative to which subnets Karpenter can launch into. See §5.

### 2.4 Storage type and drive cache — `storage_type`, `drive_cache_type`

- `storage_type = "SSD"` (default): pick this for inference. Predictable low latency, PERSISTENT_2 or PERSISTENT_1.
- `storage_type = "HDD"`: PERSISTENT_1 only. Cheap per GiB, ~10× higher latency. Requires `drive_cache_type = "READ"` or `"NONE"`.
- `storage_type = "INTELLIGENT_TIERING"`: PERSISTENT_2 only, no `storage_capacity` (Lustre sizes itself), uses `throughput_capacity` in 4000 MB/s increments. Good for cold-heavy datasets. See the [AWS blog on Intelligent Tiering for Lustre](https://aws.amazon.com/blogs/aws/new-amazon-fsx-for-lustre-intelligent-tiering-a-fully-elastic-file-storage-that-costs-up-to-96-less-for-infrequently-accessed-data/).

For an inference cluster that pins a hot working set, **stay on SSD**. Intelligent Tiering is a fit if you also do periodic bulk training and want one file system for both hot and cold data.

### 2.5 Data compression — `data_compression_type`

Allowed: `NONE` (default), `LZ4`. LZ4 is transparent and cheap; it saves storage bytes and increases effective throughput on compressible data (JSON, text tokens, sparse tensors). Model weight files (FP16/BF16/FP8 tensors, safetensors) compress poorly, so the effect is usually small. Enabling it does not hurt; a reasonable default is `LZ4`.

### 2.6 Logging — `log_configuration`

Structure:

```hcl
log_configuration {
  destination = aws_cloudwatch_log_group.fsx.arn
  level       = "WARN_ERROR"
}
```

Levels: `DISABLED`, `WARN_ONLY`, `ERROR_ONLY`, `WARN_ERROR`, `FAILURE_ONLY`. `WARN_ERROR` is the useful production default — it will surface DRA import/export errors and health events without spamming.

The log group name **must** start with `/aws/fsx/`. Retention should mirror the repo's existing pattern (`cluster_log_retention_days` from `variables.tf`).

### 2.7 Encryption — `kms_key_id`

If omitted, FSx uses the AWS-managed KMS key `aws/fsx`. Pass an ARN for a customer-managed key (`aws_kms_key.fsx.arn`). The key policy must allow FSx to `Encrypt`/`Decrypt` under the caller account (the service-linked role handles this transparently for AWS-managed keys).

### 2.8 Maintenance — `weekly_maintenance_start_time`

Format: `d:HH:MM` UTC, where `d` is day-of-week (1 = Monday). Example `7:03:00` = Sunday 03:00 UTC. Pick a time outside your peak inference window; the maintenance may briefly interrupt I/O.

### 2.9 Backups — `automatic_backup_retention_days`, `daily_automatic_backup_start_time`, `copy_tags_to_backups`

Only meaningful with `PERSISTENT_1` or `PERSISTENT_2`.

- `automatic_backup_retention_days` — `0` disables (default). `1`–`90` enables and retains for N days.
- `daily_automatic_backup_start_time` — `HH:MM` UTC. Required if retention > 0.
- `copy_tags_to_backups` — bool. Set to `true` so `DeploymentId` propagates to backups (the repo's teardown story relies on tag-based sweeping).

If your entire FSx contents are re-derivable from S3 (DRA export continuously syncs back), backups are largely redundant. Set retention to `0` unless you also store non-S3 state on Lustre.

### 2.10 File system type version — `file_system_type_version`

Allowed: `"2.10"`, `"2.12"`, `"2.15"`. Default depends on deployment type but is Lustre `2.15` for PERSISTENT_2. Only override if you need to pin because a client image ships an older `lustre-client`.

### 2.11 Restore from backup — `backup_id`

`backup_id = "backup-01234567890abcdef"` creates the file system from a snapshot. Mutually exclusive with `storage_capacity` (the size comes from the backup).

---

## 3. `aws_fsx_data_repository_association` — S3 linkage

`aws_fsx_data_repository_association` (DRA) is the mechanism that keeps a Lustre directory in sync with an S3 prefix, in both directions. Per the [Terraform docs](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/fsx_data_repository_association), DRAs are supported **only on `PERSISTENT_2`** file systems.

### 3.1 Arguments

- `file_system_id` — the FSx ID.
- `data_repository_path` — `s3://<bucket>/<prefix>/` (trailing slash matters).
- `file_system_path` — mount-relative path Lustre exposes the S3 prefix at (e.g. `/models/`). Must not overlap another DRA.
- `imported_file_chunk_size` — MiB per stripe. Default 1024. Larger = better sequential throughput; smaller = better parallelism on many small files.
- `batch_import_meta_data_on_create` — bool. Import metadata for all existing S3 objects into the Lustre namespace at creation time. Recommended `true` for pre-populated buckets.
- `delete_data_in_filesystem` — bool. On DRA delete, whether to remove the corresponding Lustre entries. Default `false`. Keep `false` unless you know why.
- `s3.auto_import_policy.events` — subset of `["NEW", "CHANGED", "DELETED"]`. What S3 object events are reflected into Lustre.
- `s3.auto_export_policy.events` — same. What Lustre changes are exported back to S3.

For the inference cluster onboarder pattern (S3 is source of truth, Lustre is a hot cache), a common setup is:

```hcl
s3 {
  auto_import_policy { events = ["NEW", "CHANGED", "DELETED"] }
  auto_export_policy { events = [] }   # workloads never write to /models
}
```

For a training checkpoint bucket that both directions matter for:

```hcl
s3 {
  auto_import_policy { events = ["NEW", "CHANGED", "DELETED"] }
  auto_export_policy { events = ["NEW", "CHANGED", "DELETED"] }
}
```

### 3.2 Multiple DRAs on one file system

You can attach multiple DRAs to the same file system as long as `file_system_path` values are disjoint. This is the natural way to combine several onboarder-managed prefixes:

```
/models         → s3://<store>/models/
/checkpoints    → s3://<store>/checkpoints/
/datasets       → s3://<store>/datasets/
```

Each association creates its own `AWSServiceRoleForFSxS3Access_<fs-id>` SLR entry.

### 3.3 IAM prerequisites

The IAM caller that creates the file system needs the permissions from [Adding permissions to use data repositories in Amazon S3](https://docs.aws.amazon.com/fsx/latest/LustreGuide/setting-up.html#fsx-adding-permissions-s3), essentially:

```json
{
  "Effect": "Allow",
  "Action": ["iam:CreateServiceLinkedRole", "iam:AttachRolePolicy", "iam:PutRolePolicy"],
  "Resource": "arn:aws:iam::*:role/aws-service-role/s3.data-source.lustre.fsx.amazonaws.com/*"
}
```

Either your Terraform caller has this baseline, or the target S3 bucket has a resource policy that already grants FSx the required actions. In the jupyter-deploy shape this is a one-time account-level concern; the running cluster's Pod Identity roles don't need it.

---

## 4. IAM: the three roles you need to reason about

Three distinct IAM pieces are in play. Each has one job.

### 4.1 `AWSServiceRoleForAmazonFSx` (service-linked)

The account-wide SLR FSx uses to manage its own ENIs, tag them, publish CloudWatch metrics, and write to `/aws/fsx/*` log groups. Documented at [Using service-linked roles for Amazon FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-service-linked-roles.html).

You do not need to create it manually — FSx creates it on the first `CreateFileSystem` call. In practice, races on greenfield accounts have been observed where the first `aws_fsx_lustre_file_system` apply fails with `InvalidServiceLinkedRole`. Pre-creating with Terraform eliminates that class of flake:

```hcl
resource "aws_iam_service_linked_role" "fsx" {
  aws_service_name = "fsx.amazonaws.com"
  description      = "SLR used by Amazon FSx"
}
```

Wrap with a `try(...)` or use `import` in accounts where it already exists — SLRs error on duplicate create. A common pattern:

```hcl
resource "aws_iam_service_linked_role" "fsx" {
  aws_service_name = "fsx.amazonaws.com"

  lifecycle {
    # Prevent recreation if imported / already present.
    ignore_changes = [aws_service_name]
  }
}
```

### 4.2 `AWSServiceRoleForFSxS3Access_<fs-id>` (per file system, per S3 link)

Auto-created by FSx per file system when you attach a DRA. FSx uses it to reach into the linked S3 bucket. **Do not create this yourself** — the name is derived at file-system creation time.

### 4.3 FSx CSI driver's controller role (Pod Identity)

The FSx CSI driver's controller Pod needs IAM permissions to call `CreateFileSystem`/`DeleteFileSystem`/`DescribeFileSystems` etc. only if you use **dynamic** provisioning (StorageClass). For **static** PVs the CSI driver only needs to mount an already-existing FS, which requires no AWS API calls.

The straightforward, blunt policy is `AmazonFSxFullAccess`:

```hcl
module "fsx_csi_role" {
  source             = "./modules/iam_role"
  role_name          = "${local.resource_name_prefix}-fsx-csi"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
  policy_arns        = ["arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonFSxFullAccess"]
  combined_tags      = local.combined_tags
}
```

Then a Pod Identity association from the controller SA to that role:

```hcl
resource "aws_eks_pod_identity_association" "fsx_csi" {
  cluster_name    = module.eks_cluster.cluster_name
  namespace       = "kube-system"
  service_account = "fsx-csi-controller-sa"
  role_arn        = module.fsx_csi_role.role_arn
  tags            = local.combined_tags

  depends_on = [aws_eks_addon.pod_identity_agent]
}
```

If you want tighter scoping for dynamic provisioning, the least-privilege set in the [CSI driver's install guide](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/blob/master/docs/install.md#set-up-driver-permission) is smaller than the managed policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "iam:CreateServiceLinkedRole",
        "iam:AttachRolePolicy",
        "iam:PutRolePolicy"
      ],
      "Resource": "arn:aws:iam::*:role/aws-service-role/s3.data-source.lustre.fsx.amazonaws.com/*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "fsx:CreateFileSystem",
        "fsx:DeleteFileSystem",
        "fsx:DescribeFileSystems",
        "fsx:TagResource",
        "fsx:UntagResource"
      ],
      "Resource": "*"
    }
  ]
}
```

For a template that is _only_ going to use static provisioning against a Terraform-managed FS, you can drop the CSI controller's IAM entirely (leave the SA un-annotated) — the driver's node plugin needs no AWS credentials to mount an existing FS. This is the most common shape in this repo's inference clusters, because Terraform is already the source of truth for infrastructure.

### 4.4 What the node role does NOT need

The EC2 instance role (`node_role` in `iam.tf`) needs **nothing** FSx-specific to mount a Lustre file system. Lustre uses standard client packages (`lustre-client-x86_64` from `amazon-linux-extras` or the AL2023 kernel modules) and identifies itself over the wire by IP + Lustre RPC — no AWS API call is involved in the mount. The security-group boundary is the entire access-control story on the data plane.

---

## 5. Subnet & AZ selection tied to Karpenter NodePools

This is the subtle part.

FSx Lustre lives in **exactly one subnet** (`subnet_ids` accepts a list of size 1). Every ENI is in that AZ. A GPU node in a different AZ can still mount, but every read/write crosses AZ — pay for the traffic and eat the latency.

Karpenter's [`EC2NodeClass.subnetSelectorTerms`](https://karpenter.sh/docs/concepts/nodeclasses/) uses tags — in this repo, `karpenter.sh/discovery = <cluster_name>` (see `modules/vpc/main.tf:57-68`). By default Karpenter can land nodes in either private subnet, so the AZ of a Karpenter-launched node is not deterministic.

You have three options, in increasing order of correctness:

### 5.1 Option A: single FSx, one FS per cluster, best-effort AZ

Simplest. One `aws_fsx_lustre_file_system` in the first private subnet. Karpenter may land nodes in either AZ; cross-AZ mounts pay a small tax. Reasonable for small clusters and low-throughput usage patterns.

```hcl
resource "aws_fsx_lustre_file_system" "shared" {
  # ...
  subnet_ids = [module.vpc.private_subnet_ids[0]]
}
```

### 5.2 Option B: pin GPU NodePool to FSx's AZ

Better. Add a `topology.kubernetes.io/zone In [<fsx-az>]` requirement to any NodePool that mounts FSx. In this repo's chart `charts/karpenter/templates/nodepools.yaml`, the `gpu-g` and `gpu-p` NodePools would gain:

```yaml
requirements:
  - key: topology.kubernetes.io/zone
    operator: In
    values: ["us-west-2a"]     # injected from Terraform
```

That means one AZ's capacity constrains the whole GPU fleet. That is a real risk on P4/P5 where reservations are AZ-scoped anyway; often acceptable.

### 5.3 Option C: one FSx per AZ, PV per AZ, workload affinity

Most correct at scale. Provision one file system per AZ Karpenter is allowed into; ship one PV per FS; use `WaitForFirstConsumer` binding and `nodeAffinity` on each PV so a pod binds to the local FS. This is what the [official EKS + FSx example](https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi-create.html) does when it recommends multi-AZ.

Sketch:

```hcl
resource "aws_fsx_lustre_file_system" "per_az" {
  for_each   = { for i, s in module.vpc.private_subnet_ids : i => s }
  subnet_ids = [each.value]
  # ...
}
```

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: fsx-lustre-a
spec:
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: topology.kubernetes.io/zone
              operator: In
              values: ["us-west-2a"]
  csi:
    driver: fsx.csi.aws.com
    volumeHandle: fs-abc123::abc12345
    volumeAttributes:
      dnsname: fs-abc123.fsx.us-west-2.amazonaws.com
      mountname: abc12345
```

For a template that already exposes two private subnets across two AZs, Option B is the pragmatic default. Option C is the target once workloads have real multi-AZ HA requirements.

### 5.4 EFA and FSx

If your P4d/P5 NodePools enable [EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html) (this repo's `platform_efa.tf`), the FSx security group rules **must** use SG-ID sources for _every_ rule that EFA touches, per the [FSx docs](https://docs.aws.amazon.com/fsx/latest/LustreGuide/limit-access-security-groups.html#efa-security-groups): "CIDR-based rules, including `0.0.0.0/0`, do not satisfy EFA requirements even if they allow all traffic on all ports." Always source-by-SG, never by CIDR, on the FSx SG when EFA is in play.

---

## 6. The security-group pair

Two SGs, one shared reference each direction.

```hcl
resource "aws_security_group" "fsx" {
  name_prefix = "${local.resource_name_prefix}-fsx-"
  description = "FSx for Lustre file-system SG"
  vpc_id      = module.vpc.vpc_id
  tags        = merge(local.combined_tags, { Name = "${local.resource_name_prefix}-fsx" })

  lifecycle {
    create_before_destroy = true
  }
}

# Lustre TCP 988 from client SG (the EKS cluster SG in this repo) to the FSx SG.
resource "aws_vpc_security_group_ingress_rule" "fsx_988_from_cluster" {
  security_group_id            = aws_security_group.fsx.id
  ip_protocol                  = "tcp"
  from_port                    = 988
  to_port                      = 988
  referenced_security_group_id = module.eks_cluster.cluster_security_group_id
  description                  = "Lustre RPC from EKS pods/nodes"
  tags                         = local.combined_tags
}

# Lustre TCP 1018-1023 from client SG to the FSx SG.
resource "aws_vpc_security_group_ingress_rule" "fsx_1018_1023_from_cluster" {
  security_group_id            = aws_security_group.fsx.id
  ip_protocol                  = "tcp"
  from_port                    = 1018
  to_port                      = 1023
  referenced_security_group_id = module.eks_cluster.cluster_security_group_id
  description                  = "Lustre reserved TCP range from EKS pods/nodes"
  tags                         = local.combined_tags
}

# Self-referencing on the FSx SG covers inter-node Lustre traffic (multi-server topology).
resource "aws_vpc_security_group_ingress_rule" "fsx_988_self" {
  security_group_id            = aws_security_group.fsx.id
  ip_protocol                  = "tcp"
  from_port                    = 988
  to_port                      = 988
  referenced_security_group_id = aws_security_group.fsx.id
  description                  = "Lustre RPC between FSx file servers (self)"
  tags                         = local.combined_tags
}

resource "aws_vpc_security_group_ingress_rule" "fsx_1018_1023_self" {
  security_group_id            = aws_security_group.fsx.id
  ip_protocol                  = "tcp"
  from_port                    = 1018
  to_port                      = 1023
  referenced_security_group_id = aws_security_group.fsx.id
  description                  = "Lustre reserved range between FSx file servers (self)"
  tags                         = local.combined_tags
}

# Egress: allow the FSx SG to reply to clients + reach itself.
resource "aws_vpc_security_group_egress_rule" "fsx_all_to_cluster" {
  security_group_id            = aws_security_group.fsx.id
  ip_protocol                  = "-1"
  referenced_security_group_id = module.eks_cluster.cluster_security_group_id
  description                  = "Allow all egress to cluster SG"
  tags                         = local.combined_tags
}

resource "aws_vpc_security_group_egress_rule" "fsx_all_self" {
  security_group_id            = aws_security_group.fsx.id
  ip_protocol                  = "-1"
  referenced_security_group_id = aws_security_group.fsx.id
  description                  = "Allow all egress to self"
  tags                         = local.combined_tags
}
```

And on the client side (the EKS cluster SG that the node ENI carries — `module.eks_cluster.cluster_security_group_id` in this repo):

```hcl
# Client SG must permit egress to the FSx SG on the same ports.
resource "aws_vpc_security_group_egress_rule" "cluster_to_fsx_988" {
  security_group_id            = module.eks_cluster.cluster_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 988
  to_port                      = 988
  referenced_security_group_id = aws_security_group.fsx.id
  description                  = "Lustre RPC to FSx"
  tags                         = local.combined_tags
}

resource "aws_vpc_security_group_egress_rule" "cluster_to_fsx_1018_1023" {
  security_group_id            = module.eks_cluster.cluster_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 1018
  to_port                      = 1023
  referenced_security_group_id = aws_security_group.fsx.id
  description                  = "Lustre reserved range to FSx"
  tags                         = local.combined_tags
}
```

Notes:

- The EKS cluster SG is what the VPC CNI attaches to every ENI on every node (managed and Karpenter alike) unless you use SGs-for-Pods. In this repo it is the right client SG.
- These rules avoid `0.0.0.0/0` sources so that EFA-enabled node pools don't need a separate SG story.
- Use the modern `aws_vpc_security_group_{ingress,egress}_rule` resources rather than inline `ingress {}` blocks on `aws_security_group`; the modern resources are per-rule and diff cleanly.

---

## 7. The complete Terraform payload

Below is a copy-pasteable `platform_fsx.tf` you can drop into `libs/inference-tf-aws-eks-karpenter/inference_tf_aws_eks_karpenter/template/engine/`. It follows the repo's conventions:

- Names every resource under `${local.resource_name_prefix}-...` (which embeds `random_id.postfix.hex`).
- Tags every resource with `local.combined_tags`.
- Wires FSx into the existing `null_resource.cluster_addons` ordering aggregator so a `jd down` unwinds cleanly.

```hcl
# === FSx for Lustre — high-throughput shared FS for inference / training ===
#
# One PERSISTENT_2 SSD file system per cluster, in the first private subnet
# (AZ selection is intentional — see research/fsx/terraform-eks-integration.md §5).
# Karpenter GPU pools are constrained to the same AZ via a topology requirement
# in charts/karpenter/templates/nodepools.yaml so mounts stay local.
# S3 linkage flows through a Data Repository Association against the shared model
# store bucket; workloads read from /models over Lustre while onboarders continue
# to write to S3 as source of truth.

locals {
  fsx_namespace = "kube-system"
  # AZ the FS lives in — pinned so it stays stable across applies and matches the
  # Karpenter GPU NodePool topology requirement.
  fsx_subnet_id = module.vpc.private_subnet_ids[0]
  fsx_mount     = "/models"       # In-FS path the DRA lands the S3 prefix at.
  fsx_s3_prefix = local.model_store_models_prefix
}

# --- Service-linked role (idempotent; safe on brownfield accounts if imported) ---

resource "aws_iam_service_linked_role" "fsx" {
  aws_service_name = "fsx.amazonaws.com"
  description      = "SLR used by Amazon FSx"

  lifecycle {
    ignore_changes = [aws_service_name]
  }
}

# --- Security group + rules (988 / 1018-1023 TCP, self and cluster SG) ---

resource "aws_security_group" "fsx" {
  name_prefix = "${local.resource_name_prefix}-fsx-"
  description = "FSx for Lustre file-system SG"
  vpc_id      = module.vpc.vpc_id
  tags        = merge(local.combined_tags, { Name = "${local.resource_name_prefix}-fsx" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "fsx_988_from_cluster" {
  security_group_id            = aws_security_group.fsx.id
  ip_protocol                  = "tcp"
  from_port                    = 988
  to_port                      = 988
  referenced_security_group_id = module.eks_cluster.cluster_security_group_id
  description                  = "Lustre RPC from EKS cluster SG"
  tags                         = local.combined_tags
}

resource "aws_vpc_security_group_ingress_rule" "fsx_1018_1023_from_cluster" {
  security_group_id            = aws_security_group.fsx.id
  ip_protocol                  = "tcp"
  from_port                    = 1018
  to_port                      = 1023
  referenced_security_group_id = module.eks_cluster.cluster_security_group_id
  description                  = "Lustre reserved range from EKS cluster SG"
  tags                         = local.combined_tags
}

resource "aws_vpc_security_group_ingress_rule" "fsx_988_self" {
  security_group_id            = aws_security_group.fsx.id
  ip_protocol                  = "tcp"
  from_port                    = 988
  to_port                      = 988
  referenced_security_group_id = aws_security_group.fsx.id
  description                  = "Lustre RPC self"
  tags                         = local.combined_tags
}

resource "aws_vpc_security_group_ingress_rule" "fsx_1018_1023_self" {
  security_group_id            = aws_security_group.fsx.id
  ip_protocol                  = "tcp"
  from_port                    = 1018
  to_port                      = 1023
  referenced_security_group_id = aws_security_group.fsx.id
  description                  = "Lustre reserved range self"
  tags                         = local.combined_tags
}

resource "aws_vpc_security_group_egress_rule" "fsx_all_to_cluster" {
  security_group_id            = aws_security_group.fsx.id
  ip_protocol                  = "-1"
  referenced_security_group_id = module.eks_cluster.cluster_security_group_id
  description                  = "Allow all egress to cluster SG"
  tags                         = local.combined_tags
}

resource "aws_vpc_security_group_egress_rule" "fsx_all_self" {
  security_group_id            = aws_security_group.fsx.id
  ip_protocol                  = "-1"
  referenced_security_group_id = aws_security_group.fsx.id
  description                  = "Allow all egress self"
  tags                         = local.combined_tags
}

# Client SG (EKS cluster SG) egress complement.
resource "aws_vpc_security_group_egress_rule" "cluster_to_fsx_988" {
  security_group_id            = module.eks_cluster.cluster_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 988
  to_port                      = 988
  referenced_security_group_id = aws_security_group.fsx.id
  description                  = "Lustre RPC to FSx"
  tags                         = local.combined_tags
}

resource "aws_vpc_security_group_egress_rule" "cluster_to_fsx_1018_1023" {
  security_group_id            = module.eks_cluster.cluster_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 1018
  to_port                      = 1023
  referenced_security_group_id = aws_security_group.fsx.id
  description                  = "Lustre reserved range to FSx"
  tags                         = local.combined_tags
}

# --- CloudWatch log group for FSx event logs ---

resource "aws_cloudwatch_log_group" "fsx" {
  name              = "/aws/fsx/${local.resource_name_prefix}"
  retention_in_days = var.cluster_log_retention_days
  tags              = local.combined_tags
}

# --- File system ---
#
# PERSISTENT_2 unlocks DRA. 4800 GiB × 250 MB/s/TiB ≈ 1.17 GB/s aggregate throughput,
# LZ4 compression on, backups disabled (S3 is the source of truth via DRA), Sunday
# 03:00 UTC maintenance window. Increase per_unit_storage_throughput to 500 or 1000
# for P4d/P5-heavy fleets or heavy training checkpoint traffic.
resource "aws_fsx_lustre_file_system" "shared" {
  storage_type                = "SSD"
  deployment_type             = "PERSISTENT_2"
  storage_capacity            = var.fsx_storage_capacity_gib
  per_unit_storage_throughput = var.fsx_per_unit_storage_throughput
  data_compression_type       = "LZ4"
  file_system_type_version    = "2.15"
  kms_key_id                  = var.fsx_kms_key_arn == "" ? null : var.fsx_kms_key_arn

  subnet_ids         = [local.fsx_subnet_id]
  security_group_ids = [aws_security_group.fsx.id]

  weekly_maintenance_start_time     = "7:03:00"
  automatic_backup_retention_days   = 0
  copy_tags_to_backups              = true

  log_configuration {
    level       = "WARN_ERROR"
    destination = aws_cloudwatch_log_group.fsx.arn
  }

  tags = merge(local.combined_tags, {
    Name = "${local.resource_name_prefix}-lustre"
  })

  depends_on = [aws_iam_service_linked_role.fsx]

  timeouts {
    create = "45m"
    update = "45m"
    delete = "45m"
  }
}

# --- Data repository association: /models ⇄ s3://<model_store>/models/ ---
#
# Import-only by default (workloads never write to /models). Flip the auto_export_policy
# on to make workload writes propagate back to S3. batch_import_meta_data_on_create
# ensures every pre-existing object under the prefix is indexed at DRA-create time.
resource "aws_fsx_data_repository_association" "models" {
  file_system_id                  = aws_fsx_lustre_file_system.shared.id
  data_repository_path            = "s3://${module.model_store.bucket_name}/${local.fsx_s3_prefix}/"
  file_system_path                = local.fsx_mount
  batch_import_meta_data_on_create = true
  imported_file_chunk_size         = 1024
  delete_data_in_filesystem        = false

  s3 {
    auto_import_policy { events = ["NEW", "CHANGED", "DELETED"] }
    auto_export_policy { events = [] }
  }

  tags = local.combined_tags

  timeouts {
    create = "30m"
    update = "30m"
    delete = "30m"
  }
}

# --- FSx CSI driver: controller IAM (Pod Identity) ---
#
# Required only if the cluster ever uses DYNAMIC provisioning (StorageClass). For a
# purely-static PV shape this can be a NoOp policy; we grant AmazonFSxFullAccess to
# keep the door open for dynamic use.
module "fsx_csi_role" {
  source             = "./modules/iam_role"
  role_name          = "${local.resource_name_prefix}-fsx-csi"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
  policy_arns        = ["arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonFSxFullAccess"]
  combined_tags      = local.combined_tags
}

# --- FSx CSI driver: Helm release ---
#
# Not published as an EKS managed addon at the time of writing — install via Helm.
# The chart is served from https://kubernetes-sigs.github.io/aws-fsx-csi-driver.
resource "helm_release" "fsx_csi_driver" {
  name       = "aws-fsx-csi-driver"
  repository = "https://kubernetes-sigs.github.io/aws-fsx-csi-driver"
  chart      = "aws-fsx-csi-driver"
  version    = var.fsx_csi_driver_chart_version
  namespace  = local.fsx_namespace

  set = [
    # Pin the controller Deployment to the tainted system NG (control-loop pattern).
    { name = "controller.nodeSelector.inference/role", value = "system" },
    { name = "controller.tolerations[0].key", value = "inference/role" },
    { name = "controller.tolerations[0].operator", value = "Equal" },
    { name = "controller.tolerations[0].value", value = "system" },
    { name = "controller.tolerations[0].effect", value = "NoSchedule" },
    # Node plugin must run on every node, incl. Karpenter GPU nodes.
    { name = "node.tolerateAllTaints", value = "true" },
    # Repoint the container image at the ECR pull-through mirror if using the
    # air-gapped posture (see local.ecr_registry / trusted_upstreams in main.tf).
    # { name = "image.repository", value = "${local.ecr_registry}/ecr-public/fsx-csi-driver/aws-fsx-csi-driver" },
  ]

  depends_on = [
    null_resource.cluster_addons,
    module.node_group,
  ]
}

resource "aws_eks_pod_identity_association" "fsx_csi" {
  cluster_name    = module.eks_cluster.cluster_name
  namespace       = local.fsx_namespace
  service_account = "fsx-csi-controller-sa"
  role_arn        = module.fsx_csi_role.role_arn
  tags            = local.combined_tags

  depends_on = [aws_eks_addon.pod_identity_agent]
}

# --- Wire the static PV/PVC through the storage chart ---
#
# Extend the existing charts/storage helm release to render an FSx PV + PVC bound
# to this file system. See §8 for the chart template; the values wired here.
# The storage helm_release in platform_storage.tf gains these set entries:
#   { name = "fsx.enabled",     value = "true" },
#   { name = "fsx.fileSystemId", value = aws_fsx_lustre_file_system.shared.id },
#   { name = "fsx.dnsName",     value = aws_fsx_lustre_file_system.shared.dns_name },
#   { name = "fsx.mountName",   value = aws_fsx_lustre_file_system.shared.mount_name },
#   { name = "fsx.capacity",    value = "${var.fsx_storage_capacity_gib}Gi" },
#   { name = "fsx.claimName",   value = "model-store-fsx" },
#   { name = "fsx.claimNamespace", value = kubernetes_namespace_v1.workload.metadata[0].name },
```

Variables to add to `engine/variables.tf`:

```hcl
variable "fsx_storage_capacity_gib" {
  description = "FSx for Lustre storage capacity in GiB. PERSISTENT_2 SSD requires multiples of 1200."

  # Larger sizes buy proportionally more aggregate throughput
  # (per_unit_storage_throughput × storage_capacity / 1024).
  # recommended: 4800
  type = number
}

variable "fsx_per_unit_storage_throughput" {
  description = "FSx for Lustre per-unit throughput in MB/s per TiB (PERSISTENT_2 SSD)."

  # 125, 250, 500, 1000. 250 is a mid-range default; bump for P4d/P5 fleets.
  # recommended: 250
  type = number
}

variable "fsx_kms_key_arn" {
  description = "Customer-managed KMS key ARN for FSx encryption at rest. Empty = AWS-managed."

  # recommended: ""
  type = string
}

variable "fsx_csi_driver_chart_version" {
  description = "aws-fsx-csi-driver Helm chart version."

  # Pin to a known-good release from the kubernetes-sigs chart repo.
  # recommended: "1.10.1"
  type = string
}
```

Defaults for `engine/presets/defaults-all.tfvars`:

```hcl
fsx_storage_capacity_gib        = 4800
fsx_per_unit_storage_throughput = 250
fsx_kms_key_arn                 = ""
fsx_csi_driver_chart_version    = "1.10.1"
```

Outputs to add to `engine/outputs.tf`:

```hcl
output "fsx_file_system_id" {
  description = "ID of the shared FSx for Lustre file system."
  value       = aws_fsx_lustre_file_system.shared.id
}

output "fsx_file_system_arn" {
  description = "ARN of the shared FSx for Lustre file system."
  value       = aws_fsx_lustre_file_system.shared.arn
}

output "fsx_dns_name" {
  description = "DNS name of the shared FSx for Lustre file system (mount source)."
  value       = aws_fsx_lustre_file_system.shared.dns_name
}

output "fsx_mount_name" {
  description = "Lustre mount name for the shared file system (second half of volumeHandle)."
  value       = aws_fsx_lustre_file_system.shared.mount_name
}

output "fsx_availability_zone" {
  description = "AZ the file system lives in — pin Karpenter GPU NodePools here."
  value       = data.aws_subnet.fsx.availability_zone
}

output "fsx_data_repository_path" {
  description = "S3 URI the /models mount is linked to via the DRA."
  value       = aws_fsx_data_repository_association.models.data_repository_path
}
```

You'll need the corresponding data source:

```hcl
data "aws_subnet" "fsx" {
  id = local.fsx_subnet_id
}
```

---

## 8. Kubernetes wiring: static PV, PVC, and (optionally) a StorageClass

Follow the repo's chart pattern. Extend `charts/storage/values.yaml`:

```yaml
# -- FSx for Lustre static PV/PVC — shared RWX file system.
fsx:
  enabled: false
  # -- FSx file system ID (fs-xxxxxxxx).
  fileSystemId: ""
  # -- DNS name (fs-xxxx.fsx.<region>.amazonaws.com).
  dnsName: ""
  # -- Mount name — the short opaque string FSx generates.
  mountName: ""
  # -- Capacity for the PV/PVC (Lustre has real capacity, unlike Mountpoint-S3).
  capacity: 4800Gi
  # -- Namespace + name of the RWX PVC charts mount.
  claimNamespace: default
  claimName: model-store-fsx
```

Add `charts/storage/templates/fsx-mount.yaml`:

```yaml
{{- /*
FSx for Lustre static PV/PVC. The FSx CSI driver supports both dynamic and static
provisioning; static is the shape a Terraform-managed FS uses. `volumeHandle` is
"<fs-id>::<mount-name>" for FSx (unlike EBS/EFS where it's just the ID).
mountOptions include `flock` for POSIX file-lock semantics — inference engines
that use SafeTensors mmap benefit from it.
*/ -}}
{{- if .Values.fsx.enabled }}
apiVersion: v1
kind: PersistentVolume
metadata:
  name: {{ .Values.fsx.claimName }}
spec:
  capacity:
    storage: {{ .Values.fsx.capacity }}
  accessModes:
    - ReadWriteMany
  storageClassName: ""
  persistentVolumeReclaimPolicy: Retain
  mountOptions:
    - flock
  csi:
    driver: fsx.csi.aws.com
    volumeHandle: {{ .Values.fsx.fileSystemId }}::{{ .Values.fsx.mountName }}
    volumeAttributes:
      dnsname: {{ .Values.fsx.dnsName | quote }}
      mountname: {{ .Values.fsx.mountName | quote }}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{ .Values.fsx.claimName }}
  namespace: {{ .Values.fsx.claimNamespace }}
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: ""
  resources:
    requests:
      storage: {{ .Values.fsx.capacity }}
  volumeName: {{ .Values.fsx.claimName }}
{{- end }}
```

Reference: [static provisioning example](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/tree/master/examples/kubernetes/static_provisioning).

### 8.1 Dynamic provisioning (optional)

If you want workloads to allocate _new_ file systems on demand (rare in this repo — Terraform is source of truth), add a StorageClass:

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: fsx-persistent-2
provisioner: fsx.csi.aws.com
reclaimPolicy: Retain
volumeBindingMode: WaitForFirstConsumer
parameters:
  subnetId: subnet-0abc123...       # must be a subnet in the target AZ
  securityGroupIds: sg-0def456...   # comma-separated list
  deploymentType: PERSISTENT_2
  storageType: SSD
  perUnitStorageThroughput: "250"
  fileSystemTypeVersion: "2.15"
  dataCompressionType: LZ4
  autoImportPolicy: NEW_CHANGED_DELETED
  copyTagsToBackups: "false"
  weeklyMaintenanceStartTime: "7:03:00"
  # extraTags propagates onto the file system for cost allocation / cleanup.
  extraTags: "DeploymentId=abc12345,Managed=jd-fsx-csi"
```

Then a PVC pulls from that class:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: scratch-fsx
spec:
  accessModes: [ReadWriteMany]
  storageClassName: fsx-persistent-2
  resources:
    requests:
      storage: 1200Gi
```

`WaitForFirstConsumer` binding is essential: it defers file-system creation until the first pod schedules, so the FS lands in the same AZ as the pod. **This makes dynamic FSx safe on a multi-AZ node fleet, whereas Immediate binding does not.**

The full StorageClass parameter list is at the [driver's dynamic-provisioning README](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/tree/master/examples/kubernetes/dynamic_provisioning).

### 8.2 What the volumeHandle actually is

For the FSx CSI driver, `volumeHandle` is **not** just the file system ID (as with EBS). It is `<fs-id>::<mount-name>`, e.g. `fs-0199e5a63bd90f796::abc12345`. `mount_name` is Lustre-specific: FSx exports it as `<fs-dns>@tcp:/<mount-name>`. Both come from Terraform outputs.

You can verify by running `aws fsx describe-file-systems --file-system-ids <id> --query 'FileSystems[0].LustreConfiguration.MountName'` — this is exactly what the AL2023 `mount.lustre` invocation ends up dialing.

### 8.3 Mount options on a static PV

- `flock` — enable POSIX file locking. Recommended for SafeTensors mmap and for engines that use `flock`/`lockf` around cache writes.
- `noatime` — skip access-time updates. Small perf win.
- `_netdev` — treated as a network filesystem by systemd. Cosmetic in a container; the CSI driver mounts via its own binary.

---

## 9. Consumer workloads: pod spec

A pod that mounts the shared FSx over the model store:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: vllm-server
  namespace: default
spec:
  tolerations:
    - key: nvidia.com/gpu
      operator: Equal
      value: present
      effect: NoSchedule
  nodeSelector:
    inference/accelerator: nvidia-g
  # Anchor pods to the same AZ as the FSx file system (Option B from §5.2).
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
          - matchExpressions:
              - key: topology.kubernetes.io/zone
                operator: In
                values: ["us-west-2a"]     # from Terraform output fsx_availability_zone
  containers:
    - name: vllm
      image: <ecr-registry>/workload/vllm:0.8.5
      resources:
        limits:
          nvidia.com/gpu: "1"
      volumeMounts:
        - name: models
          mountPath: /models
          readOnly: true
      command: ["vllm", "serve", "/models/mistral-7b-instruct"]
  volumes:
    - name: models
      persistentVolumeClaim:
        claimName: model-store-fsx
```

`readOnly: true` at the volumeMount level is enforced by the kubelet even though the PV itself is `ReadWriteMany` — it is a defense-in-depth belt. If the DRA has `auto_export_policy` disabled (as in the default config), writing to `/models` from a pod is harmless (writes never leave the FS), but for a strict weight-serving pod you want the mount read-only anyway.

---

## 10. Interactions with the rest of the platform

### 10.1 EKS Karpenter subnet tags

`modules/vpc/main.tf` tags both private subnets with `karpenter.sh/discovery = <cluster>`. If you take Option B (§5.2) and pin GPU NodePools to the FSx AZ, that tag is still fine — the topology requirement in the NodePool spec constrains AZ, not subnet count.

### 10.2 EFA on P nodes

`platform_efa.tf` enables EFA on P NodePools. When EFA is enabled, the FSx SG rules must be **SG-referenced**, not CIDR-based, per the [FSx EFA-SG doc](https://docs.aws.amazon.com/fsx/latest/LustreGuide/limit-access-security-groups.html#efa-security-groups). The template in §6 uses SG references throughout, so this composes without change.

### 10.3 VPC endpoints

FSx is a **regional service** whose data plane is the Lustre protocol (TCP 988 / 1018–1023) directly between clients and the file server ENIs in your VPC — there is no data plane through a public endpoint. The FSx **control plane** (`CreateFileSystem`, `DescribeFileSystems`, DRA management) uses the FSx API endpoint (`fsx.<region>.amazonaws.com`). If you run this template with `enable_nat_gateway = false` (endpoints-only), and you use dynamic provisioning, add `fsx` to the interface endpoint list in `modules/vpc/main.tf:local.interface_endpoints`. If you only use static provisioning (Terraform manages the FS), the CSI driver never calls the FSx API, so no endpoint is needed.

### 10.4 Ordering into the `null_resource.cluster_addons` aggregator

The repo uses a single `null_resource.cluster_addons` that every helm release depends on, so on destroy all charts uninstall before any addon is removed. Add `helm_release.fsx_csi_driver` to that graph the same way the storage chart does today — the sample above shows `helm_release.fsx_csi_driver` depending on `null_resource.cluster_addons` and `module.node_group`.

For teardown: the FSx CSI driver being present while the file system is still around is fine. What matters is that pods with FSx PVCs are gone _before_ the file system is destroyed — Terraform will handle that automatically via the PV → PVC → helm release → `aws_fsx_lustre_file_system` dependency chain, so long as any workload chart with an FSx PVC depends on the storage chart (which it already does).

### 10.5 Interaction with the s3 model store bucket

The DRA in this template points at `s3://<model_store>/models/`, which is the same prefix the onboarder writes into. Onboarder writes land in S3 → `auto_import_policy` events reflect them into Lustre → workloads on `/models` see them within seconds. This is deliberately identical semantics to the existing Mountpoint-S3 story, except with real POSIX and much higher aggregate throughput.

If you also want the batch inference output flow to hit FSx, add a second DRA at `/batch-out` pointing at the batch output bucket. Note the DRA's `auto_export_policy` events determine whether Lustre writes propagate back to S3 — set them if you want workloads to write to Lustre and have S3 be the durable copy.

---

## 11. Teardown considerations

The repo's teardown pattern (`platform_karpenter.tf: null_resource.karpenter_drain`) is exhaustive about draining Karpenter nodes so the VPC can delete cleanly. FSx interacts with two edges:

1. **FSx ENIs live in a private subnet.** Deleting the VPC before the FS is deleted fails. Terraform will get this ordering right because `aws_fsx_lustre_file_system.shared.subnet_ids` references `module.vpc.private_subnet_ids`, so on destroy Terraform deletes the FS before the subnets.
2. **The AWSServiceRoleForAmazonFSx SLR must survive until the last FS is deleted.** Terraform manages the SLR as a top-level resource with `ignore_changes = [aws_service_name]`, and the FS's implicit dependency on the SLR (via `depends_on = [aws_iam_service_linked_role.fsx]`) keeps the ordering right. On destroy Terraform will delete the FS before the SLR.
3. **DRAs delete in seconds** even for very large associations, because they only unlink the Lustre entries from their S3 references — `delete_data_in_filesystem = false` means the actual Lustre files stay in place until the FS itself is deleted, which then bulk-deletes everything atomically. This is the fast path.

If a destroy hangs, the most common cause is orphaned pods still mounting the PVC — check `kubectl get pods -o json | jq '.items[] | select(.spec.volumes[]?.persistentVolumeClaim.claimName == "model-store-fsx")'`. Force-delete the pods and Terraform will make progress.

---

## 12. Operational notes

### 12.1 Observability

FSx exports metrics under the `AWS/FSx` CloudWatch namespace:

- `DataReadBytes`, `DataWriteBytes` — data-plane throughput per file system.
- `MetadataOperations` — namespace pressure.
- `DiskReadBytes`, `DiskWriteBytes` — I/O to backing storage.
- `FreeStorageCapacity`, `LogicalDiskUsage` — capacity headroom.

Wire these into the existing Prometheus stack (`platform_prometheus.tf`) via the CloudWatch exporter or via [Container Insights for EKS](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Container-Insights.html), which the repo already installs. The FSx log group (`/aws/fsx/<cluster>`) collects DRA import/export errors — set a subscription filter if you want them to fan out to Slack.

### 12.2 Warming the cache

Newly-imported metadata from S3 is Lustre-visible but not on Lustre disk yet — the first read of each file streams from S3. For deterministic first-load latency, warm proactively:

```bash
# Prefetch every file under a prefix. Run from a node that mounts /models.
find /models -type f -exec cat {} > /dev/null \;
```

Or use the [`lfs hsm_restore`](https://docs.aws.amazon.com/fsx/latest/LustreGuide/preload-data-repository.html) command against a specific file. AWS calls this "pre-loading files from your data repository" and documents it here: [Preloading files into your file system](https://docs.aws.amazon.com/fsx/latest/LustreGuide/preload-data-repository.html).

### 12.3 Capacity growth

`storage_capacity` on PERSISTENT_2 can be increased in 1200-GiB increments in place (`fsx:UpdateFileSystem`). Terraform will submit the update and poll to completion within the `update` timeout. `per_unit_storage_throughput` is also in-place updatable on PERSISTENT_2. This means both dials are runtime-tunable without a rebuild — safe to start conservative (4800 GiB / 250 MB/s/TiB) and grow.

### 12.4 Cost sanity check

Per the [Amazon FSx for Lustre pricing page](https://aws.amazon.com/fsx/lustre/pricing/), PERSISTENT_2 SSD 250 MB/s/TiB is roughly $0.145/GiB-month in us-west-2 at the time of writing. A 4800 GiB file system therefore lands around $700/month; bump to 500 MB/s/TiB and it's ~$1400/month. Intelligent Tiering can drop the effective per-GiB cost dramatically for cold datasets — worth evaluating for datasets where the working set is much smaller than the total corpus.

---

## 13. Comparison with the rest of the FSx family

For completeness, when someone asks "should we use FSx for X on EKS":

| Product                        | Protocol      | K8s CSI driver                                                                                       | Best fit                                                       |
|--------------------------------|---------------|-------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------|
| **FSx for Lustre**             | Lustre (POSIX) | [aws-fsx-csi-driver](https://github.com/kubernetes-sigs/aws-fsx-csi-driver)                          | ML training, inference weight caching, HPC scratch, S3-backed |
| **FSx for OpenZFS**            | NFSv4          | [aws-fsx-openzfs-csi-driver](https://github.com/kubernetes-sigs/aws-fsx-openzfs-csi-driver)          | Home dirs, general-purpose NFS, snapshots + clones             |
| **FSx for NetApp ONTAP**       | NFS / SMB / iSCSI | [Trident](https://github.com/NetApp/trident) (NetApp) or [FSx NetApp CSI](https://github.com/NetApp/trident) | Multi-protocol, hybrid cloud parity                            |
| **FSx for Windows File Server**| SMB           | [smb-csi-driver](https://github.com/kubernetes-csi/csi-driver-smb) (community)                       | Legacy Windows workloads (rare on inference EKS)               |

For an ML inference cluster specifically, Lustre is the right pick 95% of the time. OpenZFS is worth a look only if the workload does a lot of small-file synchronous writes with fsync semantics and doesn't need >100 GB/s aggregate bandwidth.

---

## 14. Common pitfalls

1. **Multi-subnet input.** `subnet_ids` accepts a list but for Lustre it must have exactly one element. Passing two produces an API error at apply time — not caught in `plan`.
2. **DRA on wrong deployment type.** Attempting to attach `aws_fsx_data_repository_association` to a `PERSISTENT_1` or `SCRATCH_2` file system fails with `BadRequest: Data repository associations are only supported on PERSISTENT_2 file systems`. Always `PERSISTENT_2` if you want DRA.
3. **`volumeHandle` typo.** For the FSx CSI static PV, `volumeHandle` must be `<fs-id>::<mount-name>` (double colon) and the `mountname` volume attribute must match. Everything else about the PV can look right but the mount will hang for minutes with `mount.lustre: mount ... failed`.
4. **Cluster SG on the client, not the node SG.** In this repo the VPC CNI attaches ENIs with the EKS cluster SG. Rules on the node role SG _do nothing_ for pod traffic unless SGs-for-Pods is on. Point the FSx SG's client-side rules at `module.eks_cluster.cluster_security_group_id`.
5. **Cross-AZ mounts without noticing.** The DNS name resolves to the FSx ENIs in whatever AZ the FS lives in. A pod on a Karpenter GPU node in another AZ will mount successfully and quietly pay inter-AZ transfer per byte. Pin the AZ (§5.2) or run one FS per AZ (§5.3).
6. **Forgot the SLR on a fresh account.** First `aws_fsx_lustre_file_system` apply can race the SLR auto-create. Fix: create the SLR explicitly with `aws_iam_service_linked_role.fsx` and have the FS `depends_on` it.
7. **DRA S3 URI missing trailing slash.** `s3://bucket/prefix` and `s3://bucket/prefix/` are treated differently by the DRA API. Always include the trailing slash on the S3 side.
8. **Import didn't happen.** If `batch_import_meta_data_on_create = false` (the default), only files uploaded to S3 _after_ DRA creation appear in Lustre. Set it to `true` for pre-populated buckets, or run `create-data-repository-task` after the fact.
9. **`AmazonFSxFullAccess` alone doesn't grant the DRA SLR creation permission.** The CSI driver's controller needs the `iam:CreateServiceLinkedRole` on `aws-service-role/s3.data-source.lustre.fsx.amazonaws.com/*` if it is doing DRA management. For static provisioning this is a non-issue.
10. **EBS ordering.** The repo already establishes that the EBS CSI driver comes up before the storage chart's PVs are created. FSx is analogous — the FSx CSI driver must be up before any FSx PV binds. The `depends_on` chain from the storage chart through `null_resource.cluster_addons` covers it as long as `helm_release.fsx_csi_driver` is upstream of `helm_release.storage`.

---

## 15. Full example: end-to-end reference

Below is a minimum-viable end-to-end payload showing every piece. Drop it into a fresh sandbox to verify.

```hcl
# platform_fsx.tf — reproduce with the rest of the repo's platform_*.tf files.

# 1. SLR (idempotent)
resource "aws_iam_service_linked_role" "fsx" {
  aws_service_name = "fsx.amazonaws.com"
  description      = "SLR used by Amazon FSx"
  lifecycle { ignore_changes = [aws_service_name] }
}

# 2. Security group + rules (see §6 for the full set; abbreviated here)
resource "aws_security_group" "fsx" {
  name_prefix = "${local.resource_name_prefix}-fsx-"
  description = "FSx for Lustre file-system SG"
  vpc_id      = module.vpc.vpc_id
  tags        = merge(local.combined_tags, { Name = "${local.resource_name_prefix}-fsx" })
  lifecycle { create_before_destroy = true }
}
# ... 6 rule resources (988/1018-1023 in/out, self + cluster SG) as in §6 ...

# 3. Log group
resource "aws_cloudwatch_log_group" "fsx" {
  name              = "/aws/fsx/${local.resource_name_prefix}"
  retention_in_days = var.cluster_log_retention_days
  tags              = local.combined_tags
}

# 4. File system
resource "aws_fsx_lustre_file_system" "shared" {
  storage_type                = "SSD"
  deployment_type             = "PERSISTENT_2"
  storage_capacity            = var.fsx_storage_capacity_gib
  per_unit_storage_throughput = var.fsx_per_unit_storage_throughput
  data_compression_type       = "LZ4"
  file_system_type_version    = "2.15"
  kms_key_id                  = var.fsx_kms_key_arn == "" ? null : var.fsx_kms_key_arn
  subnet_ids                  = [module.vpc.private_subnet_ids[0]]
  security_group_ids          = [aws_security_group.fsx.id]
  weekly_maintenance_start_time = "7:03:00"
  automatic_backup_retention_days = 0
  copy_tags_to_backups           = true

  log_configuration {
    level       = "WARN_ERROR"
    destination = aws_cloudwatch_log_group.fsx.arn
  }

  tags       = merge(local.combined_tags, { Name = "${local.resource_name_prefix}-lustre" })
  depends_on = [aws_iam_service_linked_role.fsx]
}

# 5. DRA
resource "aws_fsx_data_repository_association" "models" {
  file_system_id                   = aws_fsx_lustre_file_system.shared.id
  data_repository_path             = "s3://${module.model_store.bucket_name}/${local.model_store_models_prefix}/"
  file_system_path                 = "/models"
  batch_import_meta_data_on_create = true
  imported_file_chunk_size         = 1024
  delete_data_in_filesystem        = false

  s3 {
    auto_import_policy { events = ["NEW", "CHANGED", "DELETED"] }
    auto_export_policy { events = [] }
  }

  tags = local.combined_tags
}

# 6. CSI role
module "fsx_csi_role" {
  source             = "./modules/iam_role"
  role_name          = "${local.resource_name_prefix}-fsx-csi"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
  policy_arns        = ["arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonFSxFullAccess"]
  combined_tags      = local.combined_tags
}

# 7. CSI driver
resource "helm_release" "fsx_csi_driver" {
  name       = "aws-fsx-csi-driver"
  repository = "https://kubernetes-sigs.github.io/aws-fsx-csi-driver"
  chart      = "aws-fsx-csi-driver"
  version    = var.fsx_csi_driver_chart_version
  namespace  = "kube-system"

  set = [
    { name = "controller.nodeSelector.inference/role", value = "system" },
    { name = "controller.tolerations[0].key", value = "inference/role" },
    { name = "controller.tolerations[0].operator", value = "Equal" },
    { name = "controller.tolerations[0].value", value = "system" },
    { name = "controller.tolerations[0].effect", value = "NoSchedule" },
    { name = "node.tolerateAllTaints", value = "true" },
  ]

  depends_on = [null_resource.cluster_addons, module.node_group]
}

resource "aws_eks_pod_identity_association" "fsx_csi" {
  cluster_name    = module.eks_cluster.cluster_name
  namespace       = "kube-system"
  service_account = "fsx-csi-controller-sa"
  role_arn        = module.fsx_csi_role.role_arn
  tags            = local.combined_tags
  depends_on      = [aws_eks_addon.pod_identity_agent]
}
```

Add outputs (see §7 outputs block) and extend the `helm_release.storage` `set` list to inject the file system ID, DNS name, and mount name into the storage chart. Add the `fsx-mount.yaml` chart template from §8. Add the four variables to `variables.tf` and default them in `presets/defaults-all.tfvars`.

Once applied:

```console
$ kubectl -n default get pvc model-store-fsx
NAME              STATUS   VOLUME            CAPACITY   ACCESS MODES   STORAGECLASS   AGE
model-store-fsx   Bound    model-store-fsx   4800Gi     RWX                           2m

$ kubectl -n default describe pv model-store-fsx | grep -E "(VolumeHandle|VolumeAttributes)"
VolumeHandle:      fs-0abc123def4567890::abc12345
VolumeAttributes:  dnsname=fs-0abc123def4567890.fsx.us-west-2.amazonaws.com
                   mountname=abc12345
```

A pod that mounts the PVC will show:

```console
$ kubectl exec -n default vllm-server -- mount | grep lustre
<dnsname>@tcp:/<mountname> on /models type lustre (ro,flock,lazystatfs)

$ kubectl exec -n default vllm-server -- ls /models
mistral-7b-instruct  llama-3-8b  falcon-40b   # populated via the DRA import
```

---

## 16. Verifying the wiring: quick smoke tests

1. **DNS resolves inside the VPC**:
   ```console
   $ kubectl run -it --rm dig --image=alpine --restart=Never --command -- \
       sh -c "apk add bind-tools >/dev/null && dig +short $FSX_DNS"
   10.0.12.15
   ```

2. **Port 988 reachable**:
   ```console
   $ kubectl run -it --rm nc --image=alpine --restart=Never --command -- \
       sh -c "apk add busybox-extras >/dev/null && nc -zv $FSX_DNS 988"
   $FSX_DNS (10.0.12.15:988) open
   ```

3. **PV binds, PVC binds, pod mounts**:
   ```console
   $ kubectl -n default get pv,pvc | grep fsx
   ```

4. **DRA has imported metadata**:
   ```console
   $ aws fsx describe-data-repository-associations \
       --association-ids $DRA_ID \
       --query 'Associations[0].{Lifecycle:Lifecycle,ImportedMetadata:BatchImportMetaDataOnCreate}'
   ```

5. **Throughput bench** (from a GPU node in the FSx AZ):
   ```console
   $ kubectl exec -n default vllm-server -- \
       dd if=/models/large-tensor.safetensors of=/dev/null bs=8M count=1024 status=progress
   ```

---

## 17. References

Terraform:
- [`aws_fsx_lustre_file_system`](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/fsx_lustre_file_system)
- [`aws_fsx_data_repository_association`](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/fsx_data_repository_association)
- [`aws_iam_service_linked_role`](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_service_linked_role)
- [`aws_vpc_security_group_ingress_rule`](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/vpc_security_group_ingress_rule)
- [`aws_eks_pod_identity_association`](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/eks_pod_identity_association)

AWS documentation:
- [Amazon FSx for Lustre User Guide](https://docs.aws.amazon.com/fsx/latest/LustreGuide/what-is.html)
- [Setting up FSx for Lustre](https://docs.aws.amazon.com/fsx/latest/LustreGuide/setting-up.html)
- [Using service-linked roles for Amazon FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-service-linked-roles.html)
- [File system access control with Amazon VPC (SG rules)](https://docs.aws.amazon.com/fsx/latest/LustreGuide/limit-access-security-groups.html)
- [Preloading files into your file system](https://docs.aws.amazon.com/fsx/latest/LustreGuide/preload-data-repository.html)
- [Using data repositories with Amazon FSx for Lustre](https://docs.aws.amazon.com/fsx/latest/LustreGuide/fsx-data-repositories.html)
- [EKS user guide: FSx for Lustre CSI driver](https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi.html)

CSI drivers and Helm charts:
- [`kubernetes-sigs/aws-fsx-csi-driver`](https://github.com/kubernetes-sigs/aws-fsx-csi-driver)
- [`aws-fsx-csi-driver` static provisioning example](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/tree/master/examples/kubernetes/static_provisioning)
- [`aws-fsx-csi-driver` dynamic provisioning example](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/tree/master/examples/kubernetes/dynamic_provisioning)
- [`aws-fsx-csi-driver` install docs](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/blob/master/docs/install.md)
- [`kubernetes-sigs/aws-fsx-openzfs-csi-driver`](https://github.com/kubernetes-sigs/aws-fsx-openzfs-csi-driver)

Blogs:
- [Optimize training time with FSx for Lustre and Amazon EKS](https://aws.amazon.com/blogs/storage/optimize-training-time-with-amazon-fsx-for-lustre-and-amazon-eks/)
- [New — Amazon FSx for Lustre Intelligent Tiering](https://aws.amazon.com/blogs/aws/new-amazon-fsx-for-lustre-intelligent-tiering-a-fully-elastic-file-storage-that-costs-up-to-96-less-for-infrequently-accessed-data/)
- [Best practices for using FSx for Lustre with Amazon SageMaker](https://aws.amazon.com/blogs/machine-learning/announcing-the-launch-of-new-hugging-face-llm-inference-containers-on-amazon-sagemaker/) (Lustre setup patterns for LLM workloads)

Repo cross-references:
- Repo storage today: `libs/inference-tf-aws-eks-karpenter/inference_tf_aws_eks_karpenter/template/engine/platform_storage.tf`
- VPC / subnet / cluster-SG wiring: `libs/inference-tf-aws-eks-karpenter/inference_tf_aws_eks_karpenter/template/engine/modules/vpc/main.tf`
- Karpenter NodePool chart (where the AZ topology requirement lives): `libs/inference-tf-aws-eks-karpenter/inference_tf_aws_eks_karpenter/template/charts/karpenter/templates/nodepools.yaml`
- Addon ordering aggregators: `libs/inference-tf-aws-eks-karpenter/inference_tf_aws_eks_karpenter/template/engine/eks_addons.tf`
- Existing Pod Identity trust policy source: `libs/inference-tf-aws-eks-karpenter/inference_tf_aws_eks_karpenter/template/engine/iam.tf`
