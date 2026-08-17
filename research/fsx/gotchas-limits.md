---
title: "FSx for Lustre — operational gotchas, quotas, and failure modes"
slug: gotchas-limits
audience: SRE / platform / ML-platform engineers running EKS + FSx for Lustre in production
last_reviewed: 2026-08-06
sources:
  - https://docs.aws.amazon.com/fsx/latest/LustreGuide/limits.html
  - https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-data-repos.html
  - https://docs.aws.amazon.com/fsx/latest/LustreGuide/create-dra-linked-data-repo.html
  - https://docs.aws.amazon.com/fsx/latest/LustreGuide/lustre-client-matrix.html
  - https://docs.aws.amazon.com/fsx/latest/LustreGuide/mount-troubleshooting.html
  - https://github.com/kubernetes-sigs/aws-fsx-csi-driver
---

# FSx for Lustre — operational gotchas, quotas, and failure modes

## TL;DR

- FSx for Lustre is a managed **Lustre 2.12 / 2.15** service, not a friendly POSIX filer: clients are kernel modules, LNet uses TCP/988, and misbehaving clients get **evicted** with `Cannot send after transport endpoint shutdown` — not a mount error you can retry, but a hard failure that usually requires the pod/node to be recycled.
- Almost every "immutable" property on a FSx for Lustre file system really is immutable — `DeploymentType`, `StorageType`, `KmsKeyId`, and the *subnet* / AZ of the file system cannot be changed after creation. You must destroy and re-create, and the **DNS name, IP and 8-character mount name change** on every re-creation and on many restores. Static PVs and `/etc/fstab` entries break.
- Storage-capacity increases and throughput scaling take a **6-hour cool-down** between operations; throughput scaling can leave the FS unavailable for **up to an hour**; storage capacity scaling shows a brief "UPDATING" window with retries but a long background optimization window (hours-to-days) where clients see degraded performance ([managing storage capacity](https://docs.aws.amazon.com/fsx/latest/LustreGuide/managing-storage-capacity.html), [managing throughput capacity](https://docs.aws.amazon.com/fsx/latest/LustreGuide/managing-throughput-capacity.html)).
- A single FS supports **at most 8 Data Repository Associations (DRAs)** on a Persistent 2 / Scratch 2 / Persistent 1 file system, with unique non-overlapping file-system and S3 prefixes, and the linked bucket **must be in the same Region** for auto-import ([create-dra-linked-data-repo](https://docs.aws.amazon.com/fsx/latest/LustreGuide/create-dra-linked-data-repo.html)). DRAs are **not** supported on FSx for Lustre 2.10 / Scratch 1 file systems.
- POSIX permissions on S3-imported files come from S3 object metadata; files without `x-amz-meta-file-permissions` default to **mode 0755, owner root:root**. Any tool that re-writes the S3 object without preserving `x-amz-meta-file-*` headers will silently reset UID/GID/mode on the next import ([POSIX metadata support](https://docs.aws.amazon.com/fsx/latest/LustreGuide/posix-metadata-support.html)).
- `flock` is not the default mount option; without it, applications that use POSIX advisory locks (SQLite, most `git` operations on the repo, some PyTorch/Lightning checkpointing paths, `dpkg`, `apt`, `pip`) will silently corrupt state or fail with `EBADF`/`EINVAL`. Always mount with `-o flock,relatime,_netdev` ([mounting-ec2-instance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/mounting-ec2-instance.html)).
- Lustre client kernel-module compatibility is versioned by *both* OS and *exact kernel patch version*. Bottlerocket historically shipped **without** the Lustre client kmod; EKS-managed AL2023 nodes require kernel `>=6.1.79-99.167.amzn2023` (`Lustre 2.15`); mis-matched clients fail at mount with `mount failed: exit status 19 / No such device / Are the lustre modules loaded?` ([lustre-client-matrix](https://docs.aws.amazon.com/fsx/latest/LustreGuide/lustre-client-matrix.html), [aws-fsx-csi-driver#289](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/issues/289), [aws-fsx-csi-driver#356](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/issues/356)).

---

## 1. Scope and audience

This note focuses on production operability of **Amazon FSx for Lustre** consumed from **EKS via the `aws-fsx-csi-driver`**, for ML-training / ML-inference workloads that pull weights and datasets from S3.

It is deliberately opinionated: I care about the failure modes that turn a green cluster red at 2am, and I try to name each with the exact error string you will see in `kubectl describe pod` / `dmesg`, and the shortest recovery step.

I assume you know what Lustre is (a parallel POSIX filesystem with MGS/MDT/OST roles), and that you understand roughly what a Kubernetes `PersistentVolume` and `StorageClass` do. This is not a getting-started guide.

Deployment topology assumed:

```
                +----------------+
S3 bucket <---> |  FSx-Lustre FS |
  (data repo)   |  (Persistent 2 |
                |   or IT class) |
                +--------+-------+
                         | LNet TCP/988, 1018-1023
                         |
                +--------+--------+
                |   EKS node      |
                |  (AL2023 kmod)  |
                |  kubelet+kube-  |
                |  proxy + CSI    |
                +--------+--------+
                         |
                +--------+--------+
                |    Pod          |
                |  /mnt/fsx-*     |
                +-----------------+
```

---

## 2. The service quotas you will actually hit

### 2.1 Per-account, per-Region quotas (soft; can be raised)

From [Service quotas for Amazon FSx for Lustre](https://docs.aws.amazon.com/fsx/latest/LustreGuide/limits.html):

| Quota | Default | What blows up if you hit it |
| --- | --- | --- |
| Persistent 1 file systems | 100 | `CreateFileSystem` returns `ServiceLimitExceeded` |
| Persistent 2 file systems | 100 | Same |
| Scratch file systems | 100 | Same |
| Persistent 1 storage capacity (all FSes) | 100,800 GiB | Same |
| Persistent 2 storage capacity (all FSes) | 100,800 GiB | Same |
| Persistent HDD storage capacity (per FS) | 102,000 GiB | Same |
| Scratch storage capacity | 100,800 GiB | Same |
| Intelligent-Tiering throughput capacity | 100,000 MBps | Same |
| Intelligent-Tiering SSD read cache capacity | 100,800 GiB | Same |
| User-initiated backups | 500 | `CreateBackup` fails, retention operations queue |

The two that bite hardest in ML shops:

1. **Persistent 2 storage capacity is a *pooled* quota, not per file system.** 100,800 GiB sounds huge; a small fleet of 6-TiB training FSes eats through it fast. Ask for the raise when you plan the cluster, not the day you hit it — quota-increase SLAs are days, not hours.
2. **Intelligent-Tiering caps are per account per Region.** Multi-tenant "one big FS per team" designs converge on the 100,000 MBps ceiling faster than you expect once you enable IT storage.

Request increases via the [Service Quotas console](https://console.aws.amazon.com/servicequotas/home) — filter by `Amazon FSx for Lustre`.

### 2.2 Per-file-system hard limits

From the same [service-quotas page](https://docs.aws.amazon.com/fsx/latest/LustreGuide/limits.html):

| Resource | Limit |
| --- | --- |
| Tags | 50 |
| Automatic backup retention | 90 days |
| Cross-Region backup copies in flight (per account, per destination Region) | 5 |
| Automatic S3-linked updates | 10,000,000 files / month |
| Minimum storage — SSD | 1.2 TiB |
| Minimum storage — HDD | 6 TiB |
| Throughput per unit of storage — SSD | 50–1000 MBps/TiB |
| Throughput per unit of storage — HDD | 12–40 MBps/TiB |
| KMS key reuse | up to 125 file systems per CMK |

**Gotcha 1:** the "10 million file updates from linked S3 bucket per file system per month" limit is *not* a plan-limit doc, it is enforced. Blowing past it means auto-import falls behind and eventually the DRA moves to `MISCONFIGURED`. See section 6.4.

**Gotcha 2:** you cannot make a Lustre FS below `1.2 TiB` SSD. Team topologies that assume `PersistentVolumeClaim: 100Gi` will pay for 1.2 TiB anyway — with dynamic provisioning the CSI driver rounds up.

### 2.3 Data Repository Association limits

From [Linking your file system to an Amazon S3 bucket](https://docs.aws.amazon.com/fsx/latest/LustreGuide/create-dra-linked-data-repo.html):

| Property | Value |
| --- | --- |
| DRAs per file system | **8** (was 1 on older docs; now 8 on Persistent 2 / Scratch 2 / Persistent 1) |
| Queued DRA requests | 8 (only one worked on at a time) |
| File-system path overlap | **not allowed** across DRAs |
| S3-prefix overlap | **not allowed** across DRAs |
| Cross-Region auto-import | **not supported** |
| DRAs on Scratch 1 / FSx for Lustre 2.10 | **not supported** |

If you want more than 8 buckets fronted by one FS, the workaround is (a) use one bucket with different prefixes and one DRA per prefix, (b) put a manifest object in one bucket and use `lfs hsm_restore` on demand, or (c) run multiple file systems and mount them all on the pod.

### 2.4 S3 object and file limits inside a DRA

Not always in the front-page docs, but worth pinning down:

- Maximum object size that will be surfaced as a Lustre file: **12 TiB** (matches Lustre's max stripe file size at 32 stripes × 384 GiB; empirically AWS documents Lustre 2.15 supports files up to this size).
- Object keys must be POSIX-compliant. Objects with keys containing embedded newlines, non-UTF-8 bytes, or that resolve to paths outside the DRA root are skipped ([POSIX metadata support](https://docs.aws.amazon.com/fsx/latest/LustreGuide/posix-metadata-support.html)).
- Symlinks are stored as **zero-byte objects** with the target in the object body and `Content-Type: application/symlink; charset=utf-8`. Importing then round-tripping a symlink through non-Lustre-aware S3 tooling (aws-cli, boto3 `copy_object` without preserving metadata) permanently corrupts it into a zero-byte regular file.
- Import metadata via a "load metadata from repository" or a Data Repository Task ([data-repository-tasks](https://docs.aws.amazon.com/fsx/latest/LustreGuide/data-repository-tasks.html)). Import tasks can process billions of files but partition failures do not abort the task: read the completion report from the `s3://<bucket>/task-report/` prefix or the CloudWatch Logs group.

### 2.5 Ports and networking

From [File system access control with Amazon VPC](https://docs.aws.amazon.com/fsx/latest/LustreGuide/limit-access-security-groups.html):

| Direction | Protocol | Port | Notes |
| --- | --- | --- | --- |
| Client → FS | TCP | 988 | LNet main port |
| Client → FS | TCP | 1018–1023 | Reserved-port RPCs |
| FS ↔ FS | TCP | 988, 1018–1023 | Between OSSes / MDS |
| EFA-enabled | all | all | Must reference SG-ID, not CIDR |

The `1018-1023` reserved-port range is a Lustre convention: the client kernel must bind an outbound TCP from a privileged port. If you have a stateful firewall or a container that runs unprivileged and tries to open reserved ports, the mount succeeds but reads/writes fail with `mount.lustre: Cannot allocate reserved port`.

---

## 3. The 8-character mount name and why it will break you

Every FSx for Lustre file system has an 8-character alphanumeric identifier — the **Lustre "fsname"** — which is embedded in the client's mount command. AWS calls it the `MountName` in [`DescribeFileSystems`](https://docs.aws.amazon.com/fsx/latest/APIReference/API_DescribeFileSystems.html), or `mountname` in most docs. Example: `aqhs7bev`, `7w4uvbmv`.

The mount name shows up in the `mount(8)` command:

```bash
# Correct
sudo mount -t lustre -o relatime,flock,_netdev \
  fs-08a962c9c8001462f.fsx.us-west-2.amazonaws.com@tcp:/aqhs7bev  \
  /mnt/fsx
```

### 3.1 Why it matters

The 8-char name is *the* first thing the client hands the MGS. Get it wrong and the mount fails right away with:

```
mount.lustre: mount fs-0123....fsx.us-east-1.aws@tcp:/fsx at /lustre
failed: No such file or directory

Is the MGS specification correct?
Is the filesystem name correct?
```

(from [mount-troubleshooting.html — "File system mount fails right away"](https://docs.aws.amazon.com/fsx/latest/LustreGuide/mount-troubleshooting.html)).

### 3.2 When it changes

It changes on any operation that provisions a *new* backend file system. In particular:

1. **Restoring a backup into a new file system**: the new FS gets a new `MountName`. Any `/etc/fstab` line, static `PersistentVolume`, or Terraform-templated Helm value that hard-codes the old one is now broken.
2. **Deleting and re-creating** (obviously).
3. **Migrating between deployment types** (which is a create + copy, not an in-place update).

It does **not** change on:

- Storage-capacity increases (the FS stays put; new OSSes are attached transparently).
- Throughput-capacity changes (SSD or Intelligent-Tiering).
- Maintenance-window patching (see section 7).
- Auto-import / auto-export operations.

### 3.3 Terraform hygiene

If you use Terraform to publish the mount name to Kubernetes (for a static PV) or SSM Parameter Store, wire it as an *output*, not a hard-coded local — and let the module owning the FS emit it. Don't stash the mount name in ConfigMaps that are managed separately from the FS lifecycle.

```hcl
# libs/inference-tf-aws-eks-karpenter/engine/modules/fsx/main.tf
resource "aws_fsx_lustre_file_system" "training" {
  storage_capacity            = var.fsx_capacity_gib
  subnet_ids                  = [var.subnet_id]
  deployment_type             = "PERSISTENT_2"
  per_unit_storage_throughput = var.fsx_throughput_mbps_per_tib

  # ... immutable fields; see section 5.2
}

output "fsx_mount_name" {
  value       = aws_fsx_lustre_file_system.training.mount_name
  description = "Regenerated on FS re-creation. Consumers must not cache."
}

output "fsx_dns_name" {
  value       = aws_fsx_lustre_file_system.training.dns_name
  description = "Also regenerated; do not embed in fstab or PV YAML by hand."
}
```

---

## 4. Mount-option minefield: `flock`, `noatime`, `_netdev`

The [documented mount recipe](https://docs.aws.amazon.com/fsx/latest/LustreGuide/mount-fs-auto-mount-onreboot.html) is:

```
{{dns}}@tcp:/{{mountname}}  /fsx  lustre  defaults,relatime,flock,_netdev,x-systemd.automount,x-systemd.requires=network.service  0 0
```

For Amazon Linux 2023 and Ubuntu 22.04+, replace `x-systemd.requires=network.service` with `x-systemd.requires=systemd-networkd-wait-online.service`.

### 4.1 `flock` (or `noflock`)

FSx for Lustre defaults to **no cluster-wide file locking**. If you mount without `flock`, every `flock(2)` call returns `EBADF`, and every `fcntl(F_SETLK...)` call is silently a no-op (advisory locks work per-process, not cluster-wide).

Applications that will bite you:

| Tool | What it does wrong | Symptom |
| --- | --- | --- |
| `git` | `git-index.lock` via `flock` | `fatal: Unable to create '.git/index.lock': File exists.` on second concurrent operation, or silent index corruption |
| `sqlite3` | `flock`-based rollback journal | `database is locked` errors that never clear; or worse, silent DB corruption under concurrent writers |
| `apt` / `dpkg` | `/var/lib/dpkg/lock*` via `flock` | dpkg wedges; you cannot install packages into a container image staged on the FS |
| PyTorch `torch.distributed.FileStore` | `flock` on rendezvous file | Rank-0 acquires the lock, remaining ranks time out; training hangs |
| Ray, RLlib checkpoint writers | `flock` on `checkpoint.tmp` | Concurrent-write corruption between actors |
| MLflow local tracking | `flock` on `meta.yaml` | Race on run creation, orphaned runs |

**Just always mount with `flock`.** The performance cost is negligible for the workloads that need it, and there is no reason to try `noflock`. Grep your Helm charts.

### 4.2 `relatime` vs `noatime`

The docs [call out](https://docs.aws.amazon.com/fsx/latest/LustreGuide/mounting-ec2-instance.html) that Lustre `atime` updates travel over the wire. `relatime` is the pragmatic default: `atime` is updated only when the file has been modified since the last read, or once every 6 hours. `noatime` is a performance win for read-heavy training jobs.

Do not use `noatime` on file systems where you rely on **Intelligent-Tiering** to move cold data — the tiering engine uses last-access time to move data between Frequent Access, Infrequent Access, and Archive Instant Access tiers ([using-fsx-lustre](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html)). `noatime` freezes files in their current tier.

Do not use `noatime` if you use FSx for Lustre's *file release* feature (`lfs hsm_release`) to spill cold data to S3 — release logic is driven by atime.

### 4.3 `_netdev`

Without `_netdev`, systemd will bring up the mount before the network is initialised at boot, and the instance will hang. From the AWS docs — the recovery is *"contact AWS Support"*. It's easier not to lose this option. On EKS, kubelet mounts happen via the CSI driver so this only matters on bare-EC2 clients, but be careful with `DaemonSet` pods that also add fstab entries via `hostPath` — those *do* run through systemd.

### 4.4 Full recommended mount line (EKS StorageClass)

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: fsx-sc-training
provisioner: fsx.csi.aws.com
parameters:
  subnetId: subnet-0abcd1234efgh
  securityGroupIds: sg-0abcd1234efgh
  deploymentType: PERSISTENT_2
  perUnitStorageThroughput: "500"
  fileSystemTypeVersion: "2.15"
  # DRA
  autoImportPolicy: NEW_CHANGED_DELETED
  s3ImportPath: s3://my-training-data/
  s3ExportPath: s3://my-training-data/
mountOptions:
  - flock
  - relatime
  - _netdev
reclaimPolicy: Delete
volumeBindingMode: Immediate
```

`mountOptions` on the `StorageClass` is honoured by both static and dynamic provisioning in [`aws-fsx-csi-driver`](https://github.com/kubernetes-sigs/aws-fsx-csi-driver).

---

## 5. Immutability and deployment types

### 5.1 Deployment types at a glance

From [Deployment and storage class options](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html):

| Type | Best for | Replicated? | Backups? | DRAs? | In-place throughput scaling? |
| --- | --- | --- | --- | --- | --- |
| `SCRATCH_1` | Temporary, single-AZ, cheap | No | No | No | No — cannot scale storage either |
| `SCRATCH_2` | Temporary, higher throughput | No | No | Yes | No |
| `PERSISTENT_1` | Legacy long-lived (SSD/HDD) | Yes | Yes (SSD) | Yes | Yes |
| `PERSISTENT_2` | Current long-lived SSD | Yes | Yes (unless S3-linked) | Yes | Yes |
| `PERSISTENT_2` + Intelligent-Tiering | Elastic, cache-friendly | Multi-AZ | Yes | Yes | Yes (only increase) |

### 5.2 What is immutable after `CreateFileSystem`

- `DeploymentType`
- `StorageType` (SSD vs HDD vs Intelligent-Tiering)
- `KmsKeyId`
- `SubnetIds` (and therefore the AZ, for SSD/HDD; Intelligent-Tiering is multi-AZ but the subnets are still fixed at create time)
- `FileSystemTypeVersion` (Lustre 2.10 / 2.12 / 2.15)
- `PerUnitStorageThroughput` is mutable, `StorageCapacity` is mutable *up*, everything else structural is not.

There is *no in-place migration* between deployment types. To move from `SCRATCH_2` to `PERSISTENT_2`:

```
1. Snapshot data to S3 via a Data Repository Task (export)
2. Create new PERSISTENT_2 FS in the same VPC/subnet
3. Attach a DRA to the same S3 bucket, load metadata
4. Repoint the StorageClass / PV / consumers
5. Delete the old FS after cutover
```

### 5.3 Storage-capacity scaling

From [Managing storage capacity](https://docs.aws.amazon.com/fsx/latest/LustreGuide/managing-storage-capacity.html):

- **Increase only** — you cannot decrease storage.
- Must use one of the increments AWS presents in the console/API (usually 100% or the FS-specific base).
- **6-hour cool-down** between requests.
- File system is transiently `UPDATING` for a "few minutes"; clients auto-retry.
- Optimization (rebalancing across new OSSes) runs for **hours to days**, degrading performance while it runs.
- Scratch 1 file systems cannot be scaled at all.

### 5.4 Throughput-capacity scaling

From [Managing provisioned throughput capacity](https://docs.aws.amazon.com/fsx/latest/LustreGuide/managing-throughput-capacity.html):

- SSD-based Persistent: values are `50 / 100 / 200` (P1) or `125 / 250 / 500 / 1000` (P2) MBps/TiB. Can increase or decrease.
- Intelligent-Tiering: 4,000 MBps increments up to 2,000,000 MBps. **Increase only.**
- **File system can be unavailable for up to an hour** during the switchover. AWS bills you the new tier once available.
- **6-hour cool-down** between changes, or until optimization completes, whichever is longer.
- Not supported on EFA-enabled SSD file systems.
- SSD read cache scales automatically at 5 GiB per MBps of throughput on P2 SSD (default "Proportional to throughput").

### 5.5 Combined scaling + backup

- Backup in progress + scaling requested: scaling waits.
- Scaling in progress + backup requested: backup waits for the "optimization" phase.
- Backup wins on tie ([managing-storage-capacity#storage-capacity-changes-and-backups](https://docs.aws.amazon.com/fsx/latest/LustreGuide/managing-storage-capacity.html)).

Runbook: never schedule an ML training job that expects consistent throughput within the 24-hour window after a scaling operation.

---

## 6. Data Repository Associations — the sharp corners

### 6.1 The `MISCONFIGURED` state

DRAs are stateful. See [Data repository association lifecycle state](https://docs.aws.amazon.com/fsx/latest/LustreGuide/dra-lifecycles.html). The lifecycle is:

```
CREATING -> AVAILABLE -> UPDATING/DELETING
                 |
                 v
           MISCONFIGURED  (auto-import halted)
                 |
                 v
           FAILED         (recover by re-creation)
```

**MISCONFIGURED** is the state you will actually see. Causes ([autoimport-data-repo-dra](https://docs.aws.amazon.com/fsx/latest/LustreGuide/autoimport-data-repo-dra.html)):

1. Someone deleted or edited the `FSx` event-notification configuration on the S3 bucket. Both auto-import and auto-export rely on it.
2. FSx no longer has bucket-level permissions (`s3:GetBucketAcl`, `s3:PutBucketNotificationConfiguration`, `s3:GetBucketNotificationConfiguration`).
3. The `AgeOfOldestQueuedMessage` metric has exceeded **14 days** because the change rate on S3 exceeds what auto-import can drain.

Recovery: `aws fsx update-data-repository-association --association-id dra-...` with no changes, or console → "Update". Fully rebuild only if data drift is fatal — an import DRT does not synchronize *deletes*, so if your workflow depends on S3-side deletions you must re-create the file system.

### 6.2 Rate limits

AWS quotes: *"For an FSx for Lustre file system connected to an S3 bucket with a single shard continuously sending the maximum number of possible changes from S3, with only automatic import running on the FSx for Lustre file system, automatic import can process a 7-hour backlog of S3 changes within 14 days."* ([autoimport-data-repo-dra](https://docs.aws.amazon.com/fsx/latest/LustreGuide/autoimport-data-repo-dra.html)).

Meaning: single-bucket bulk uploads (Snowball, DataSync, big `aws s3 sync`) can permanently overrun auto-import. Options:

1. Turn auto-import off for the duration, then run an [import Data Repository Task](https://docs.aws.amazon.com/fsx/latest/LustreGuide/data-repository-tasks.html).
2. Rebuild the file system after the bulk upload completes.

### 6.3 Import policy semantics

`AutoImportPolicy` is a subset of `{NEW, CHANGED, DELETED}`. `NEW_CHANGED_DELETED` is the console default; the CLI / SDK / Terraform default is `NONE`. Almost every production template should set the value explicitly.

### 6.4 Auto-import will *not* propagate

- S3 Object Lifecycle expirations.
- Permanent deletion of a current version in a versioning-enabled bucket.
- Undelete of an object in a versioning-enabled bucket.

Design implication: versioning + auto-import is a footgun. Prefer the ancient AWS pattern of "immutable objects, keyed by content hash" for anything you want to appear in Lustre.

### 6.5 POSIX metadata semantics on import

From [POSIX metadata support](https://docs.aws.amazon.com/fsx/latest/LustreGuide/posix-metadata-support.html):

| S3 object header | Meaning |
| --- | --- |
| `x-amz-meta-file-permissions` | Octal type + mode, e.g. `0100664` |
| `x-amz-meta-file-owner` | UID as integer |
| `x-amz-meta-file-group` | GID as integer |
| `x-amz-meta-file-atime` | ns since epoch, `ns` suffix |
| `x-amz-meta-file-mtime` | ns since epoch, `ns` suffix |

If missing:

- Default mode: **0755** (files and directories both).
- Default owner: **root** (UID 0).
- setuid is **stripped**. FSx does not import or retain `setuid` bits, ever.
- POSIX ACLs are not retained.

Anything that writes to the bucket without setting `Metadata: {...}` on the PUT will land as `root:root 0755` on the Lustre side — including `aws s3 cp` without `--metadata`, boto3 `put_object` without metadata, and *any* CopyObject that omits `MetadataDirective=REPLACE` (which strips metadata by default).

Runbook: if your training container runs as UID 1000 (`fsgroup: 1000` in the pod spec), you must either

- ensure all objects have `x-amz-meta-file-owner=1000` and `x-amz-meta-file-group=1000` before they land, or
- chown from a privileged init container after mount, or
- run the app as root (defeats other controls).

The [POSIX permissions walkthrough](https://docs.aws.amazon.com/fsx/latest/LustreGuide/attach-s3-posix-permissions.html) gives the exact CLI incantation:

```bash
aws s3 cp s3cptest.txt s3://bucket/prefix/s3cptest.txt \
  --metadata '{
    "user-agent":"aws-fsx-lustre",
    "file-atime":"1595002920000000000ns",
    "file-owner":"1000",
    "file-permissions":"0100664",
    "file-group":"1000",
    "file-mtime":"1595002920000000000ns"
  }'
```

Note: it's `file-permissions`, `file-owner`, `file-group` in the SDK metadata dictionary (the SDK adds the `x-amz-meta-` prefix on the wire).

### 6.6 S3 SSE-KMS

Server-side-encrypted (SSE-KMS) buckets require the FSx service to have `kms:Decrypt` / `kms:GenerateDataKey` on the CMK. Failure mode is silent: files appear in listings, but `read()` returns `EIO` and the client is eventually evicted. Fix: add the FSx service role to the CMK's key policy — see [Working with server-side encrypted Amazon S3 buckets](https://docs.aws.amazon.com/fsx/latest/LustreGuide/s3-server-side-encryption-support.html).

---

## 7. Maintenance windows

From [maintenance-windows.html](https://docs.aws.amazon.com/fsx/latest/LustreGuide/maintenance-windows.html):

- 30-minute window per week; default is picked by AWS if not set.
- Actual patching takes a fraction of that.
- File system is transiently unavailable; clients retry.
- **The in-memory cache is erased**, so the first few minutes after maintenance show elevated latency.
- If you shift the window such that no window falls within 14 days, AWS **will patch anyway** to keep the fleet compliant.

Runbook:

- Set the window to match your quietest period (usually early Sunday for us-east-1 / us-west-2 workloads).
- Avoid Friday nights — cache re-warm on Saturday morning will collide with your first ETL run.
- Alarm on `FreeStorageCapacity` and `MetadataIOPS` immediately after the window to catch the "we've never done a real workload since patching" surprise.

### 7.1 Blast radius

The blast radius of a maintenance window is *the entire file system*. There is no per-OSS partial availability. This is why we recommend one FS per training run / one FS per team, not one big shared FS across the whole org.

---

## 8. Backups

From [using-backups-fsx.html](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-backups-fsx.html):

| Property | Value |
| --- | --- |
| Supported on | `PERSISTENT_1`, `PERSISTENT_2`, Intelligent-Tiering **only when not linked to an S3 DRA** |
| Supported on Scratch | **No** |
| Supported on P1/P2 linked to S3 | **No** (S3 is treated as the durable store) |
| Backup type | Block-level incremental |
| Storage backend | S3 (managed by AWS) |
| Durability | 11 9's |
| Automatic retention | 0–90 days (0 = disabled) |
| User-initiated retention | Never expires (indefinite) |
| User-initiated quota | 500 per account per Region |
| Cross-Region copy in flight | 5 per destination Region per account |
| Delete of backup after FS deletion | Backup persists |
| AWS Backup integration | Yes, marked `AWS_BACKUP` type |

**Gotcha:** Restoring a backup creates a *new* file system with a *new* `MountName`, *new* DNS, and *new* IPs. See section 3.2. Any static PV pointing at the old FS is now pointing at nothing.

**Gotcha:** AWS Backup–created backups cannot be deleted from the FSx console. You must delete them from the AWS Backup console (or via `aws backup delete-recovery-point`).

**Gotcha:** Backups do **not** contain the DRA configuration. If you restore a backup, you must re-create every DRA. There is no `--include-dra-associations` flag.

---

## 9. Client kernel-module compatibility (the EKS AMI trap)

This is the single most common outage cause I see. The Lustre client is a kernel module; the module must be built against the exact kernel patch version. AWS publishes the compatibility matrix at [Lustre file system and client kernel compatibility](https://docs.aws.amazon.com/fsx/latest/LustreGuide/lustre-client-matrix.html) — reproduced and annotated for EKS below.

### 9.1 The condensed matrix

| OS | Kernel line | Min kernel | Lustre client | FS 2.10 | FS 2.12 | FS 2.15 |
| --- | --- | --- | --- | --- | --- | --- |
| Amazon Linux 2023 | 6.18 | any | 2.15 | no | yes | yes |
| Amazon Linux 2023 | 6.12 | any | 2.15 | no | yes | yes |
| Amazon Linux 2023 | 6.1 | `6.1.79-99.167` | 2.15 | no | yes | yes |
| Amazon Linux 2 | 5.10 | `5.10.144-127.601` | 2.12 | yes | yes | yes |
| Amazon Linux 2 | 5.4 | `5.4.214-120.368` | 2.12 | yes | yes | yes |
| Amazon Linux 2 | 4.14 | `4.14.294-220.533` | 2.12 | yes | yes | yes |
| Ubuntu 22.04 | 6.8.0-1017+ | see docs | 2.15 | no | yes | yes |
| Ubuntu 20.04 | 5.15+ | see docs | 2.12 | yes | yes | yes |
| RHEL / Rocky 9.x | 5.14.0-70+ | version-locked per minor | 2.15 | no | yes | yes |
| RHEL / Rocky 8.x | 4.18.0-305+ | version-locked per minor | 2.12 | yes | yes | yes |

Full matrix: [lustre-client-matrix.html](https://docs.aws.amazon.com/fsx/latest/LustreGuide/lustre-client-matrix.html).

Notes:

- **RHEL EUS kernels are not supported.** Only the standard BaseOS repo kernels have a matching Lustre client.
- **Ubuntu 24 requires an `-aws-64k` variant on Graviton with 64 KB pages.** `lustre-client-modules-aws-64k` is a different package from `lustre-client-modules-aws`.
- **Amazon Linux 2 kernel 4.14 pre-`.294-220.533` has only Lustre 2.10 support** — it cannot mount a modern Persistent 2 file system.

### 9.2 EKS AMI implications

For EKS clusters, you have three practical choices:

#### Amazon Linux 2023 (recommended)

Recent AL2023 EKS-optimized AMIs ship a kernel ≥ 6.1.79-99.167 and include `lustre-client` in the repo (`dnf install -y lustre-client`). The FSx CSI driver's node DaemonSet performs `modprobe lustre` on start.

Verify from a running node:

```bash
uname -r        # want 6.1.79-99.167.amzn2023 or later
rpm -qa | grep lustre-client
lsmod | grep lustre
```

If the module is not present, the CSI node pod's `fsx-plugin` container logs will show:

```
mount.lustre: mount ... failed: No such device
Are the lustre modules loaded?
Check /etc/modprobe.conf and /proc/filesystems
```

#### Amazon Linux 2 (legacy)

`amazon-linux-extras install lustre` gives you the Lustre 2.12 client. Watch for the kernel drift: if you take an old AMI (kernel < 5.10.144-127.601), you'll get Lustre 2.10, which cannot mount Persistent 2 file systems created with `FileSystemTypeVersion=2.15`.

Bake the client into your custom AMI; do not rely on user-data-time `yum install` on cold-start nodes (adds ~30s to node ready, more if the repo is slow).

#### Bottlerocket

Bottlerocket historically has **no built-in Lustre client**. From [aws-fsx-csi-driver#289](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/issues/289) and [aws-fsx-csi-driver#356](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/issues/356) and the upstream Bottlerocket [#3459](https://github.com/bottlerocket-os/bottlerocket/issues/3459): FSx for Lustre **does not work on Bottlerocket** out of the box, because the Lustre kernel module is not bundled and you cannot install arbitrary kmods on Bottlerocket.

**If your EKS cluster uses Bottlerocket, do not plan for FSx for Lustre PVs on the same nodes.** Either:

- Split the node pool: Bottlerocket for CPU workers, AL2023 for GPU/training workers that need FSx.
- Use Karpenter's `NodePool.spec.template.spec.requirements` with `karpenter.k8s.aws/instance-family` and OS selectors to force the FSx-consuming pods onto AL2023 nodes.

Example Karpenter node pool template for training nodes that need Lustre:

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: gpu-training-fsx
spec:
  template:
    metadata:
      labels:
        workload: training-fsx
    spec:
      requirements:
        - key: karpenter.k8s.aws/instance-family
          operator: In
          values: ["p5", "p4d", "g6"]
        - key: karpenter.k8s.aws/instance-generation
          operator: Gt
          values: ["4"]
        - key: karpenter.k8s.aws/ami-family
          operator: In
          values: ["AL2023"]
      taints:
        - key: workload
          value: training-fsx
          effect: NoSchedule
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: gpu-training-fsx-al2023
```

### 9.3 Kernel-update ordering

A common outage: cluster gets Karpenter'd onto a fresh AL2023 minor with a *newer* kernel; the Lustre kmod in the AMI is still the old one; `depmod` fails to match; `modprobe lustre` returns `Module lustre not found`. Symptom: `mount failed: No such device`.

Fix in the AMI build:

```bash
# in your AMI build (packer / imagebuilder)
dnf update -y kernel
KERNEL=$(rpm -q --qf '%{VERSION}-%{RELEASE}' kernel-core | tail -1)
dnf install -y "kmod-lustre-client-${KERNEL}" || dnf install -y lustre-client
depmod -a
```

Prefer *pinning* the kernel version in the AMI and only rebuilding when the FSx compatibility matrix changes.

---

## 10. When a Lustre client loses the network

Lustre clients are stateful. A tail-latency network blip can cause an **eviction**.

### 10.1 The eviction protocol

- The server maintains a session with each client via LNet keepalives.
- If the client fails to reply to health probes for the OBD timeout (default 100s on the server side), the server evicts the client.
- Once evicted, the client's in-memory locks on the OST and metadata state are void. **All open file descriptors on that client become permanently unusable.**
- Symptoms on the client (from [aws-fsx-csi-driver#169](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/issues/169)):

```
$ touch /fsx/ping
touch: setting times of '/fsx/ping': Input/output error

$ ls -alh /fsx/ping
ls: cannot access '/fsx/ping': Cannot send after transport endpoint shutdown

$ echo hello > /fsx/ping2
bash: echo: write error: Input/output error
```

- `dmesg` on the client:

```
LustreError: 11-0: fs-abcd12345-OST0000-osc-XXXXX: operation ost_read to node 10.0.1.5@tcp failed: rc = -107
LustreError: Skipped N previous similar messages
Lustre: fs-abcd12345-OST0000-osc-XXXXX: Connection to fs-abcd12345-OST0000 (at 10.0.1.5@tcp) was lost; in progress operations using this service will wait for recovery to complete
Lustre: fs-abcd12345-OST0000-osc-XXXXX: This client was evicted by fs-abcd12345-OST0000; in progress operations using this service will fail.
```

### 10.2 Recovery

On the client side, eviction is **not** transparent — there is no automatic re-establish that re-hydrates your file descriptors. You must:

1. `umount` the file system.
2. `mount -t lustre ...` it again.
3. Restart processes that had open handles.

Under Kubernetes, the practical recovery is to kill the pod and let it be rescheduled. The CSI driver will re-issue a fresh mount.

### 10.3 What triggers evictions in practice

- Bursty network loss between the node ENI and the FSx ENI (e.g. VPC route table flap, security-group change that removes ephemeral responses, cross-AZ traffic going through an over-loaded NAT). FSx for Lustre wants stable *sub-second* RTT.
- Node under extreme memory pressure — Lustre kernel threads get starved, OBD ping deadline missed.
- Container CPU throttling on `fsx-csi-node` (the DaemonSet). If you set aggressive CPU limits on the CSI plugin, the plugin can miss its own housekeeping and the LNet layer will time out. Do not set CPU limits on `fsx-csi-node`.
- Enabling `noop-oflag` or aggressive `tc` shaping on the node.

### 10.4 Metrics to alarm on

- Node-side: `dmesg | grep -i "was evicted"` — scrape via node-exporter's `textfile` collector or systemd-journald pipeline.
- FS-side: CloudWatch metric `ClientConnections` — a drop while workload is running is a signal.
- FS-side: `MetadataIOPS` and `DataReadBytes` going to zero from a specific client IP.

---

## 11. The CSI driver: what breaks and how

Repo: [`kubernetes-sigs/aws-fsx-csi-driver`](https://github.com/kubernetes-sigs/aws-fsx-csi-driver). At the time of writing the active branch is v1.9.x, targeting Kubernetes ≥ 1.20.

### 11.1 The FS-deleted-out-of-band failure

**What happens:** somebody deletes the FSx file system in the AWS Console, or a Terraform teardown races with a live Kubernetes cluster and destroys the FS while pods still reference the `PersistentVolume`.

- New pods that try to schedule against the PV fail immediately with `MountVolume.SetUp failed for volume "pvc-...": rpc error: code = Internal desc = Filesystem is not ready`.
- Existing pods that already have the mount keep working until their client is evicted (usually within seconds — the servers are gone).
- On eviction, all pods on that node produce `-107 / Cannot send after transport endpoint shutdown` and stay wedged (see section 10).
- `NodeUnpublishVolume` may block indefinitely because `umount(2)` blocks on the missing MDS. See [aws-fsx-csi-driver#495](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/issues/495) — the CSI operation lock is per-volume and is held for the duration of the syscall, so *no* new volume operations succeed on that node until the lock is released.

**Recovery:**

1. Force-delete the affected pods: `kubectl delete pod --force --grace-period=0 <pod>`.
2. Force-delete the PVCs.
3. Delete the PVs (`kubectl delete pv <pv>` with `finalizers` cleared if needed).
4. If a CSI node DaemonSet pod is wedged (see aws-fsx-csi-driver#495), restart it: `kubectl -n kube-system delete pod fsx-csi-node-<hash> --force --grace-period=0`.
5. Re-create the file system, PVC, PV.

**Prevention:**

- Set `reclaimPolicy: Retain` on any StorageClass that fronts precious data. (For dynamic provisioning, the CSI driver still passes through the reclaim policy.)
- Use IAM policy statements to require `aws:CalledVia` = `fsx.amazonaws.com` for `fsx:DeleteFileSystem` (i.e. only allow the CSI service account, not humans).
- Set an FSx *backup* retention (only works for non-DRA-linked file systems).
- Tag the FS with `TerminationProtection=true` and enforce via SCP if you can.

### 11.2 Static-provisioning pitfalls

For static PVs the CSI driver needs the FS ID, mount name, and DNS name embedded in the PV spec. Example:

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: fsx-training-static
spec:
  capacity:
    storage: 4800Gi
  accessModes:
    - ReadWriteMany
  mountOptions:
    - flock
    - relatime
    - _netdev
  csi:
    driver: fsx.csi.aws.com
    volumeHandle: fs-08a962c9c8001462f
    volumeAttributes:
      dnsname: fs-08a962c9c8001462f.fsx.us-west-2.amazonaws.com
      mountname: aqhs7bev
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
```

Gotchas:

- If you regenerate the FS (backup restore, Terraform re-create) and forget to update `dnsname` and `mountname`, the static PV is silently pointing at the old FS — best case an immediate mount failure ([mount fails right away](https://docs.aws.amazon.com/fsx/latest/LustreGuide/mount-troubleshooting.html#mount-fails-right-away)), worst case (if the old FS still exists and you were trying to switch) writing to the wrong file system.
- `volumeHandle` must be unique across static PVs in the cluster. Reusing the same FS in two PVs (for two subdirectories) requires unique volumeHandles that still reference the same FS — the typical pattern is `fs-08a....#subpath1`, `fs-08a....#subpath2`.
- The CSI driver does not currently support mounting different subdirectories of the same FSx into different volumes via `subPath` at CSI level — see [aws-fsx-csi-driver#247](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/issues/247). Use `subPath` at the Pod level after mounting the root.

### 11.3 Dynamic-provisioning pitfalls

- The `StorageClass` accepts **exactly one** `subnetId`. See [driver docs](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/blob/master/docs/README.md). This is an FSx API constraint. If you have multi-AZ node pools, you get an FS in exactly one of them; cross-AZ mounts still work but pay cross-AZ data transfer.
- The driver creates the FS synchronously and waits (`WaitForFileSystemAvailable`) — Persistent 2 file systems take **~10 minutes** to become available. Under bursty load, the controller pod's gRPC deadline is 5 minutes, so you'll see:

```
rpc error: code = DeadlineExceeded desc = stream terminated by RST_STREAM with error code: CANCEL
```

The provisioner retries, and in the pathological case creates *another* FS on each retry. See [aws-fsx-csi-driver#433](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/issues/433): the driver "keeps continuously creating FSx for Lustre file systems until the subnet runs out of IPs." Mitigations:

  1. Set `--csi-provisioner-timeout=15m` on the external-provisioner sidecar.
  2. Pre-provision the FS (static PV) instead of relying on dynamic.

- `deletionPolicy: Delete` on the StorageClass deletes the FS on PVC delete. Combined with `--force --grace-period=0` on a runaway pod, this can nuke a training FS by accident. Use `Retain`.

### 11.4 Mount-name mistakes visible in CSI logs

```
mount failed: exit status 32
mount.lustre: mount fs-XXX.fsx.us-east-1.amazonaws.com@tcp:/oldname at ... failed: No such file or directory
Is the MGS specification correct?
Is the filesystem name correct?
```

→ the FS ID doesn't match its actual `MountName`. Fix the PV.

```
mount failed: No such device
Are the lustre modules loaded?
```

→ kernel module not loaded on the node. Fix the AMI (section 9).

```
mount failed: exit status 5
mount.lustre: mount fs-XXX...@tcp:/... failed: Input/output error
```

→ security group misconfigured (988/1018-1023 not open) or IAM policy prevents ENI creation.

---

## 12. Runbook: "if X happens, do Y"

### 12.1 "The pod is stuck in ContainerCreating"

1. `kubectl describe pod <pod> | tail -30` — look at the last `MountVolume.SetUp failed` event.
2. If the error is `No such device`: the Lustre kmod is not present. Verify:
   ```bash
   NODE=$(kubectl get pod <pod> -o jsonpath='{.spec.nodeName}')
   kubectl debug node/$NODE -it --image=alpine -- lsmod | grep lustre
   ```
   Fix: rebuild AMI or use `kubectl node drain` + re-image.
3. If the error is `No such file or directory / Is the filesystem name correct?`: the mount name in the PV is wrong. Reconcile with `aws fsx describe-file-systems --file-system-ids fs-...`.
4. If the error is `Connection timed out`: SG on either side is wrong. Verify TCP 988 and 1018-1023 are open. Fix by adding rules per section 2.5.
5. If nothing in the events makes sense: `kubectl -n kube-system logs ds/fsx-csi-node -c fsx-plugin --tail=200 --previous`.

### 12.2 "Existing pods can't access files, `Input/output error`"

1. Ssh (or `kubectl debug`) to the node.
2. `dmesg | tail -50 | grep -i lustre`.
3. If you see `was evicted`: the client was evicted (section 10.1). Recovery: `kubectl delete pod <affected pods> --grace-period=30`. The pods reschedule with a fresh mount.
4. If you see `Connection to ... was lost`: transient network. Give it 60s; if the pod recovers on its own, done. If not, delete the pod.
5. If you see nothing in dmesg but userspace still gets `EIO`: check `df -h` on the FS from the node — the FS may be full.
6. If `df` reports `Transport endpoint is not connected`: the mount is dead. `sudo umount -f -l /var/lib/kubelet/pods/.../volumes/kubernetes.io~csi/pvc-.../mount` and force-delete the pod.

### 12.3 "A CSI node DaemonSet is wedged; new pods on that node can't get FSx mounts"

Symptoms: `kubectl exec -n kube-system fsx-csi-node-<hash> -c fsx-plugin -- ls` hangs, or logs show endless `An operation with the given volume=... is already in progress` ([aws-fsx-csi-driver#369](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/issues/369)).

Fix (from [aws-fsx-csi-driver#495](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/issues/495)):

```bash
kubectl -n kube-system delete pod fsx-csi-node-<hash> --force --grace-period=0
```

The daemonset re-launches the pod, in-memory locks reset.

If a `umount(2)` is *still* stuck (D-state), you likely need to cordon and reboot the node:

```bash
kubectl cordon $NODE
kubectl drain $NODE --delete-emptydir-data --ignore-daemonsets --force
# then reboot the node via SSM or EC2
```

### 12.4 "FSx capacity is 90% full"

1. Confirm via `df -h` on a client.
2. Decide whether to grow storage (~1 hour of "UPDATING" + hours of background optimization) or release cold data via `lfs hsm_release`.
3. If growing: `aws fsx update-file-system --file-system-id fs-... --storage-capacity <new>`.
4. If shrinking is required — you can't shrink. Delete data or migrate to a new FS.

### 12.5 "DRA moved to `MISCONFIGURED`"

1. `aws fsx describe-data-repository-associations --association-ids dra-...` — get the `FailureDetails`.
2. If `Message` mentions `event notification` or `NotificationConfiguration`: check the bucket's notifications for a config named `FSx`. Do not remove it. Re-create via `update-data-repository-association`.
3. If `Message` mentions permissions: fix the IAM policy on the S3 bucket (must allow the FSx service principal to `GetBucketAcl`, `PutBucketNotificationConfiguration`, `GetBucketNotificationConfiguration`).
4. If `Message` mentions `AgeOfOldestQueuedMessage > 14 days`: throughput exceeded auto-import capacity. You have to accept that changes in S3 during the misconfigured window are lost. Bring the DRA back to `AVAILABLE`, run an [import Data Repository Task](https://docs.aws.amazon.com/fsx/latest/LustreGuide/data-repository-tasks.html) to re-scan the bucket. Note: **DRTs do not synchronize deletes**. If deletes matter, you must rebuild the FS.

### 12.6 "The FS was deleted by mistake; restore from backup"

1. `aws fsx describe-backups --filters Name=file-system-id,Values=fs-<deleted>`. Backups persist after FS deletion.
2. `aws fsx create-file-system-from-backup --backup-id backup-... --lustre-configuration ... --subnet-ids subnet-... --security-group-ids sg-...`.
3. **Note the new `MountName` in the response.**
4. Update every PV / ConfigMap / StorageClass that referenced the old FS. This is why we recommend PVs be Terraform-managed with outputs, not hand-edited YAML.
5. Re-create every DRA on the new FS (backups don't carry DRAs).
6. Trigger a metadata-only import DRT to re-populate the file listing without hydrating file data.

### 12.7 "Mount hangs on boot; instance is unresponsive"

You forgot `_netdev` in `/etc/fstab`. From [AWS docs](https://docs.aws.amazon.com/fsx/latest/LustreGuide/mount-troubleshooting.html#lustre-automount-fails): the official recovery is "contact AWS Support" if you cannot access the instance. Pragmatically, you can attach the root volume to a rescue instance, edit `/etc/fstab`, and reattach. This is why we don't put FSx mounts in fstab on EKS at all — the CSI driver mounts on demand.

---

## 13. Terraform / IaC hygiene for FSx-on-EKS

### 13.1 Never hard-code the mount name

```hcl
# BAD
resource "kubernetes_persistent_volume" "fsx" {
  spec {
    persistent_volume_source {
      csi {
        driver        = "fsx.csi.aws.com"
        volume_handle = "fs-08a962c9c8001462f"
        volume_attributes = {
          dnsname   = "fs-08a962c9c8001462f.fsx.us-west-2.amazonaws.com"
          mountname = "aqhs7bev"   # will drift on backup restore
        }
      }
    }
  }
}
```

```hcl
# GOOD
resource "kubernetes_persistent_volume" "fsx" {
  spec {
    persistent_volume_source {
      csi {
        driver        = "fsx.csi.aws.com"
        volume_handle = aws_fsx_lustre_file_system.this.id
        volume_attributes = {
          dnsname   = aws_fsx_lustre_file_system.this.dns_name
          mountname = aws_fsx_lustre_file_system.this.mount_name
        }
      }
    }
  }

  lifecycle {
    # If the FSx is replaced, the PV must be replaced too — do not try to
    # update-in-place because Kubernetes rejects most spec changes on PVs.
    replace_triggered_by = [
      aws_fsx_lustre_file_system.this.id,
    ]
  }
}
```

### 13.2 Guard against accidental deletion

```hcl
resource "aws_fsx_lustre_file_system" "training" {
  storage_capacity            = 4800
  subnet_ids                  = [var.subnet_id]
  deployment_type             = "PERSISTENT_2"
  per_unit_storage_throughput = 500

  tags = {
    Name                    = "training-fsx-${random_id.postfix.hex}"
    "aws:iam:no-delete"     = "true"   # picked up by an SCP
    DeploymentId            = random_id.postfix.hex
  }

  lifecycle {
    prevent_destroy = true
  }
}
```

Pair with an SCP:

```json
{
  "Sid": "DenyFSxDeleteWithoutBreakglass",
  "Effect": "Deny",
  "Action": ["fsx:DeleteFileSystem", "fsx:DeleteBackup"],
  "Resource": "*",
  "Condition": {
    "StringNotEquals": {
      "aws:PrincipalTag/breakglass": "true"
    }
  }
}
```

### 13.3 Prefer per-workload file systems over one shared FS

Reasons already covered:

- Maintenance-window blast radius (section 7.1).
- DRA cap of 8 buckets per FS (section 2.3).
- No decrease of storage (section 5.3).
- No in-place deployment-type migration (section 5.2).
- Backup takes minutes-to-hours; separate FS = separate backup timing (section 8).

Terraform: one module invocation per workload, drop the module into your Karpenter node-pool namespace.

---

## 14. Observability checklist

CloudWatch metrics worth alarms on:

| Metric | Reason |
| --- | --- |
| `FreeStorageCapacity` | Fill / IO errors on write |
| `DataReadBytes` / `DataWriteBytes` | Sudden zero = client evictions in progress |
| `MetadataIOPS` | If saturated, small-file workloads stall |
| `AgeOfOldestQueuedMessage` (DRA) | Auto-import falling behind |
| `ClientConnections` | Drops indicate evictions |
| `FileServerDiskThroughputUtilization` | Approaching provisioned tier |
| `PhysicalDiskReadOps` / `WriteOps` | Baseline observability |

Node-level (via node-exporter or CloudWatch Agent):

- `dmesg` for `Lustre.*evicted`, `LNetError`, `LustreError.*was lost`.
- `mount | grep lustre` present on the node.
- `sysctl -a | grep lustre` for tunings (rare, but expose for support cases).

Kubernetes-level (via kube-state-metrics or Prometheus):

- `csi_plugin_operations_seconds{driver="fsx.csi.aws.com"}` — CSI operation timing. Long P99 on `NodeUnpublishVolume` predicts stuck umounts.
- Events: `FailedMount` reason for `fsx` in the past N minutes.

---

## 15. Concise "do this / don't do this" list

Do:

- Always mount with `flock,relatime,_netdev`.
- Bake the Lustre kmod into the AMI that matches the exact kernel patch. Pin the kernel.
- Use one FSx per workload where feasible. Retain-policy PVs.
- Emit `mount_name`, `dns_name`, and `id` as Terraform outputs and wire them into PVs / CSIStorageClasses.
- Alarm on `AgeOfOldestQueuedMessage`, `ClientConnections`, `FreeStorageCapacity`.
- Test failure modes in a staging FS before you learn them in prod: kill a CSI node pod, cordon a node during a bulk-import, force an FS delete on an isolated env.
- Set `--csi-provisioner-timeout=15m` on the external-provisioner sidecar for dynamic provisioning.
- Pin the `FileSystemTypeVersion` you build for (usually `2.15` in 2026).

Don't:

- Don't mix Bottlerocket and FSx for Lustre on the same node pool.
- Don't hard-code `mount_name` or DNS in Helm charts / static YAML.
- Don't use `noflock` — ever.
- Don't put `/fsx` in the fstab of an EC2 node that also has EKS-managed CSI-mounted PVs — you'll double-mount.
- Don't schedule storage-capacity or throughput scaling within 6 hours of a previous one.
- Don't use versioned S3 buckets with auto-import DRAs unless you have a specific reason and understand section 6.4.
- Don't set CPU limits on `fsx-csi-node`. Requests only.
- Don't use `deletionPolicy: Delete` for training/experiment file systems that hold precious data.

---

## 16. Reference index

Primary AWS docs (all under `docs.aws.amazon.com/fsx/latest/LustreGuide/`):

- [Service quotas for Amazon FSx for Lustre](https://docs.aws.amazon.com/fsx/latest/LustreGuide/limits.html)
- [Deployment and storage class options](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html)
- [Linking your file system to an Amazon S3 bucket (DRA)](https://docs.aws.amazon.com/fsx/latest/LustreGuide/create-dra-linked-data-repo.html)
- [Automatically import updates from your S3 bucket](https://docs.aws.amazon.com/fsx/latest/LustreGuide/autoimport-data-repo-dra.html)
- [POSIX metadata support for data repositories](https://docs.aws.amazon.com/fsx/latest/LustreGuide/posix-metadata-support.html)
- [Attaching POSIX permissions when uploading objects into an S3 bucket](https://docs.aws.amazon.com/fsx/latest/LustreGuide/attach-s3-posix-permissions.html)
- [Data repository tasks](https://docs.aws.amazon.com/fsx/latest/LustreGuide/data-repository-tasks.html)
- [Managing storage capacity](https://docs.aws.amazon.com/fsx/latest/LustreGuide/managing-storage-capacity.html)
- [Managing provisioned throughput capacity](https://docs.aws.amazon.com/fsx/latest/LustreGuide/managing-throughput-capacity.html)
- [Mounting from an EC2 instance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/mounting-ec2-instance.html)
- [Mounting your file system automatically (fstab)](https://docs.aws.amazon.com/fsx/latest/LustreGuide/mount-fs-auto-mount-onreboot.html)
- [Troubleshooting file system mount issues](https://docs.aws.amazon.com/fsx/latest/LustreGuide/mount-troubleshooting.html)
- [Installing the Lustre client](https://docs.aws.amazon.com/fsx/latest/LustreGuide/install-lustre-client.html)
- [Lustre file system and client kernel compatibility](https://docs.aws.amazon.com/fsx/latest/LustreGuide/lustre-client-matrix.html)
- [Amazon FSx for Lustre maintenance windows](https://docs.aws.amazon.com/fsx/latest/LustreGuide/maintenance-windows.html)
- [Protecting your data with backups](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-backups-fsx.html)
- [File system access control with Amazon VPC](https://docs.aws.amazon.com/fsx/latest/LustreGuide/limit-access-security-groups.html)
- [Working with server-side encrypted Amazon S3 buckets](https://docs.aws.amazon.com/fsx/latest/LustreGuide/s3-server-side-encryption-support.html)

CSI driver:

- Repo: [kubernetes-sigs/aws-fsx-csi-driver](https://github.com/kubernetes-sigs/aws-fsx-csi-driver)
- Install docs: [docs/install.md](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/blob/master/docs/install.md)
- Key issues:
  - [#495 NodeUnpublishVolume hangs when umount(2) blocks](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/issues/495)
  - [#433 Filesystem is not ready → controller creates FSes until subnet IPs exhausted](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/issues/433)
  - [#395 OBD devices not always removed on umount](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/issues/395)
  - [#380 GRPC errors: operation already in progress](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/issues/380)
  - [#369 CSI driver stuck: An operation with the given volume is already in progress](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/issues/369)
  - [#357 Unable to mount FSx from peered VPCs](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/issues/357)
  - [#356 EKS 1.28 (Bottlerocket) does not support Lustre](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/issues/356)
  - [#289 Sample pod stuck in ContainerCreating on Bottlerocket](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/issues/289)
  - [#247 Add a subdirectory provisioner mechanism](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/issues/247)
  - [#169 Cannot send after transport endpoint shutdown](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/issues/169)

Upstream:

- Bottlerocket issue [#3459 — Lustre support](https://github.com/bottlerocket-os/bottlerocket/issues/3459)
- [Lustre wiki — Compiling Lustre](http://wiki.lustre.org/Compiling_Lustre)

---

## Appendix A: full mount-flag table

From [mount-fs-auto-mount-onreboot](https://docs.aws.amazon.com/fsx/latest/LustreGuide/mount-fs-auto-mount-onreboot.html):

| Option | Effect |
| --- | --- |
| `defaults` | rw, suid, dev, exec, auto, nouser, async |
| `relatime` | atime only when file has changed since last atime, else once per 24h (Lustre) |
| `atime` | strict atime — every read touches atime, network traffic on every read |
| `noatime` | no atime updates; incompatible with Intelligent-Tiering age-based tiering |
| `flock` | POSIX advisory file locking works cluster-wide (essential) |
| `noflock` | file locking is a no-op (default) |
| `_netdev` | systemd waits for network before attempting mount |
| `x-systemd.automount` | mount on first access, not at boot |
| `x-systemd.requires=network.service` | AL2 |
| `x-systemd.requires=systemd-networkd-wait-online.service` | AL2023 / Ubuntu 22.04+ |
| `lazystatfs` | fills df output non-blocking (default in FSx client) |
| `nofail` | do not block boot if mount fails |

## Appendix B: minimal reproduction — deliberate mount-name mismatch

Useful when writing chaos tests:

```bash
FS_ID=fs-08a962c9c8001462f
BAD_MOUNTNAME=aaaaaaaa   # not the real one
DNS=$FS_ID.fsx.us-west-2.amazonaws.com

sudo mount -t lustre -o flock,relatime,_netdev \
  $DNS@tcp:/$BAD_MOUNTNAME  /mnt/fsx

# expected:
# mount.lustre: mount ...@tcp:/aaaaaaaa at /mnt/fsx
# failed: No such file or directory
# Is the MGS specification correct?
# Is the filesystem name correct?
```

Now with the correct one from `aws fsx describe-file-systems --file-system-ids $FS_ID | jq -r '.FileSystems[0].LustreConfiguration.MountName'`:

```bash
GOOD=$(aws fsx describe-file-systems --file-system-ids $FS_ID \
       | jq -r '.FileSystems[0].LustreConfiguration.MountName')

sudo mount -t lustre -o flock,relatime,_netdev \
  $DNS@tcp:/$GOOD  /mnt/fsx

# should succeed
df -h /mnt/fsx
```

## Appendix C: minimal reproduction — forced eviction

Not portable, but useful when writing chaos tests: temporarily block port 988 on the client while a workload writes to the FS.

```bash
# On the node
sudo iptables -I OUTPUT -p tcp --dport 988 -j DROP
# Wait 120s while a pod is writing
sudo iptables -D OUTPUT -p tcp --dport 988 -j DROP

# Now on the pod
touch /mnt/fsx/probe
# expect: "Cannot send after transport endpoint shutdown"

# dmesg on the node:
# LustreError: ... was evicted by fs-...-OST0000; in progress operations using this service will fail.
```

Recovery: `kubectl delete pod --grace-period=30`. The CSI driver re-mounts on the next pod.

---

*End of note.*
