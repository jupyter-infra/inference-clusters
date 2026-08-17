---
title: FSx for Lustre — architecture, internals, and how it actually works
slug: lustre-architecture
audience: platform / infra / EKS
last_reviewed: 2026-08-06
---

# FSx for Lustre — architecture, internals, and how it actually works

## TL;DR

- FSx for Lustre is a managed distribution of the open-source **Lustre** parallel file system. AWS runs the metadata servers (MDS/MDT), object storage servers (OSS/OST), and the Lustre network (LNet) fabric; you consume it as a POSIX-mounted file system from EC2, ECS, or EKS. See [What is Amazon FSx for Lustre?](https://docs.aws.amazon.com/fsx/latest/LustreGuide/what-is.html).
- There are four generations of deployment types — **SCRATCH_1**, **SCRATCH_2**, **PERSISTENT_1** (SSD + HDD), and **PERSISTENT_2** (SSD + Intelligent-Tiering). Persistent 2 SSD supports per-unit throughput of **125 / 250 / 500 / 1000 MB/s per TiB** and Elastic Fabric Adapter (EFA)-enabled data paths for GPU workloads. See [Deployment and storage class options](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html).
- Data is **striped** across OSTs (default progressive layout since Aug 2023: `-E 100M -c 1 -E 10G -c 8 -E 100G -c 16 -E -1 -c 32`), so reads and writes to a single large file fan out over many storage targets and OSSes in parallel. See [Striping data in your file system](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html#striping-data).
- Encryption at rest is **XTS-AES-256** with KMS-backed keys (AWS-managed key for scratch, AWS- or customer-managed for persistent). Encryption in transit is automatic on **Scratch 2 and Persistent** file systems when accessed from EC2 instance types that support it. See [Data encryption in Amazon FSx for Lustre](https://docs.aws.amazon.com/fsx/latest/LustreGuide/encryption-fsxl.html).
- From an EKS pod, access is via the [aws-fsx-csi-driver](https://github.com/kubernetes-sigs/aws-fsx-csi-driver): the CSI node plugin runs `mount -t lustre <dns>@tcp:/<mountname> /mnt/fsx -o defaults,relatime,flock,_netdev,...` on the EC2 host and bind-mounts it into the pod's mount namespace.
- **Backups** apply only to **persistent** file systems that are **not** linked to an S3 data repository; backups are block-level incremental, stored in S3 at 11 nines durability, and restore into a *new* file system. Scratch has no backup and no replication.

---

## 1. Why Lustre, and what FSx actually provides

Lustre (originally "Linux + Cluster") is a POSIX-compliant, object-based parallel file system built for HPC — it decouples **metadata** (namespace, inodes) from **bulk data** (file content) and lets clients talk to many storage nodes at once, so aggregate throughput and IOPS scale with the number of storage targets. Traditional NFS/EFS-style filers push all traffic through a single (or small) set of servers; Lustre has clients open a file with one round trip to a metadata server, then read/write striped chunks directly to potentially dozens of object storage servers over an RDMA-capable network. The upstream project lives at [lustre.org](http://lustre.org/) and its manual at [doc.lustre.org](https://doc.lustre.org/lustre_manual.xhtml).

FSx for Lustre is AWS's managed offering: you POST a `CreateFileSystem` call with a deployment type, storage capacity, throughput per unit of storage, and VPC/subnet/security-group, and the service:

1. Provisions MDS/MDT and OSS/OST fleets on EC2-class hardware inside AWS's service VPC.
2. Attaches elastic network interfaces (ENIs) into *your* VPC subnets to give clients a routable target.
3. Configures LNet in TCP mode (`@tcp`) or EFA mode (`@efa`) depending on the deployment type.
4. Optionally provisions an SSD read cache in front of HDD or Intelligent-Tiering storage.
5. Registers the file system with a DNS name of the form `fs-01234567.fsx.<region>.amazonaws.com` and a **mount name** (a 7-character opaque token like `fsx1234`).

The rest of this document walks through the Lustre internals that make this work, then how AWS exposes and hardens each concept.

---

## 2. Lustre concepts — MDS/MDT, OSS/OST, LNet, LOV, striping

Understanding what FSx charges you for and why perf scales the way it does requires understanding four types of components. This section paraphrases the [Lustre Manual](https://doc.lustre.org/lustre_manual.xhtml) and cross-references FSx-specific behaviour.

### 2.1 Metadata Server (MDS) and Metadata Target (MDT)

The **MDS** is the process that serves the file-system namespace — `open`, `stat`, `readdir`, `mkdir`, `unlink`, permission checks, and layout lookups. It does *not* touch file data. Persistent metadata (inodes, directory entries, extended attributes, layout pointers) is stored on an **MDT**, which is an ldiskfs- or ZFS-backed block device.

FSx auto-provisions MDS instances behind the scenes:

- On **Persistent 2** SSD and Persistent 2 Intelligent-Tiering file systems, you can now **provision Metadata IOPS independently** of storage capacity. AWS provisions "a metadata server for every 12,000 Metadata IOPS" ([IP addresses for file systems](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html#ip-addesses-for-fs)). Valid provisioned values for SSD are `1500, 3000, 6000, 12000` and multiples of 12000 up to 192000; Intelligent-Tiering supports 6000 or 12000.
- Under the hood this is Lustre's **Distributed Namespace Environment (DNE)** feature — the namespace is sharded across multiple MDTs, allowing horizontal metadata scaling. AWS calls this "DNE" in their `dne-metadata-performance` docs anchor (see [File system metadata performance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html#dne-metadata-performance)).
- In **Automatic mode** (SSD only) the number of Metadata IOPS scales with storage capacity: 1200 GiB → 1500 IOPS, 2400 GiB → 3000, 4800–9600 GiB → 6000, 12000–45600 GiB → 12000, ≥48000 GiB → +12000 per 24000 GiB.

Cost of a metadata operation, per provisioned IOPS:

| Operation | Ops/sec per provisioned Metadata IOPS |
|---|---|
| File create, open, close | 2 |
| File delete | 1 |
| Directory create, rename | 0.1 |
| Directory delete | 0.2 |

Source: [File system metadata performance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html#dne-metadata-performance). Practically, "directory create" is an order of magnitude more expensive than "file open" — a workload that untars a giant tree of tiny directories will spend most of its time here.

### 2.2 Object Storage Server (OSS) and Object Storage Target (OST)

**OSS** processes serve file content. Each OSS owns one or more **OSTs**, which are block volumes formatted with ldiskfs (in AWS's case, since Lustre 2.10+/2.12+ — AWS ships a hardened fork of the 2.12/2.15 line).

FSx sizes the OSS fleet by dividing your provisioned capacity by a per-deployment "storage per OSS" constant. From [IP addresses for file systems](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html#ip-addesses-for-fs):

| Deployment | Throughput tier (MB/s per TiB) | Storage per OSS |
|---|---|---|
| **Persistent 2 EFA** (SSD) | 125 | 38.4 TiB per OSS |
| **Persistent 2 EFA** (SSD) | 250 | 19.2 TiB per OSS |
| **Persistent 2 EFA** (SSD) | 500 | 9.6 TiB per OSS |
| **Persistent 2 EFA** (SSD) | 1000 | 4.8 TiB per OSS |
| **Persistent 2 non-EFA** (SSD) | 125, 250, 500, 1000 | 2.4 TiB per OSS |
| **Persistent 1 SSD** | 50, 100, 200 | 2.4 TiB per OSS |
| **Persistent 1 HDD** | 12 | 6 TiB per OSS |
| **Persistent 1 HDD** | 40 | 1.8 TiB per OSS |
| **Scratch 2** | 200 | 2.4 TiB per OSS |
| **Scratch 1** | 200 | 3.6 TiB per OSS |
| **Intelligent-Tiering** | 4000 MB/s per OSS (fixed) | up to 512 TiB per OSS |

The important implication: **for the same total capacity, a higher throughput tier fans data across more OSSes.** A 96 TiB Persistent 2 EFA file system at 1000 MB/s/TiB spins up 20 OSSes (96 / 4.8). At 125 MB/s/TiB it spins up ~3 OSSes (96 / 38.4). The former gives you 96 TB × 1000 MB/s = **96 GB/s aggregate** disk throughput; the latter 12 GB/s.

### 2.3 LNet — the Lustre network

**LNet** is Lustre's abstraction over the transport. Historically it supports LNDs (Lustre Network Drivers) for InfiniBand (`o2ib`), Omni-Path, TCP (`tcp`), and (recently on AWS) EFA (`efa`). Each server and client is identified by a **Network Identifier (NID)** of the form `<ip>@<lnd>`; e.g. `10.0.1.42@tcp` or `10.0.1.42@efa`.

In FSx:

- The file system's DNS name resolves to the **primary** MDS ENI, which the mount uses as its `mgs` (management server, colocated with MDS in FSx). Once mounted, the client learns the full NID list of MDSes and OSSes from the MGS log.
- For **non-EFA** file systems the LND is `tcp` on **port 988** (default Lustre TCP port; you must open sg-to-sg 988 both directions). For **EFA-enabled** Persistent 2 (needed above 10 GBps aggregate throughput) the client uses the `@efa` LND, which requires the [`libfabric` EFA provider](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html) and the `aws-fsx-openzfs` / `aws-fsx-efa` client build. Setup instructions in [Configuring EFA clients](https://docs.aws.amazon.com/fsx/latest/LustreGuide/configure-efa-clients.html).

### 2.4 LOV — Logical Object Volume, and striping

The client-side Lustre kernel module aggregates the OSTs into a single **Logical Object Volume (LOV)**. When a client opens a file:

1. The MDS returns the file's inode plus its **layout EA (extended attribute)** — an ordered list of (OST index, object ID) tuples plus stripe size.
2. The client's LOV layer maps a byte offset `off` in the file to `(ost_index[i], object_id[i], off_in_object)` where `i = (off / stripe_size) mod stripe_count`.
3. All I/O then goes directly from the client to the OSSes owning those OSTs; the MDS is not in the data path.

FSx has evolved the default striping policy several times ([Striping data in your file system](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html#striping-data)):

- Pre-2020-12-18: `stripe_count=1` for every new file (bad for large-file throughput).
- 2020-12-18 → 2023-08-25: files < 1 GiB stripe on 1 OST; larger files use stripe count 5.
- Post 2023-08-25: **Progressive File Layout (PFL)** default:
  ```text
  lfs setstripe -E 100M -c 1 -E 10G -c 8 -E 100G -c 16 -E -1 -c 32 /fsx
  ```
  Which means files ≤ 100 MiB stay on 1 OST, up to 10 GiB use 8 stripes, up to 100 GiB use 16 stripes, and beyond 100 GiB stripe across 32 OSSes.

Files imported from an S3 data repository ignore the FS default and instead use `ImportedFileChunkSize` (default 1 GiB): a 10 GiB imported object is broken into `(10 / 1) + 1 = 11` stripes.

You can inspect and change per-file/per-directory striping with `lfs`:

```bash
# Inspect a file's stripe layout
lfs getstripe /fsx/mydir/model.ckpt

# Set a PFL layout on a directory (applies to new files created inside)
lfs setstripe -E 1G -c 4 -E -1 -c -1 /fsx/mydir

# Restripe an existing file across all OSTs
lfs migrate --stripe-count -1 /fsx/mydir/model.ckpt
```

`-c -1` means "all OSTs available in the file system." Appending to a file created under a PFL layout will populate all layout components, so any file created with `O_APPEND` gets a forced stripe count of 1 (AWS calls this out explicitly in [Progressive file layouts](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html#striping-pfl) — log files are the archetype).

### 2.5 Locking model

Lustre uses the **Lustre Distributed Lock Manager (LDLM)** to serialise concurrent access to inodes and byte ranges. This is what makes concurrent multi-node writes to the same file coherent (POSIX `read-after-write` on the same file, unlike some object stores).

The `flock` mount option (see below) additionally enables `flock(2)` / `fcntl(F_SETLK)` **advisory** file locking across clients — required for many databases, `sqlite`, and workloads that rely on lockfiles. Without `flock` these calls fail with `ENOSYS`.

---

## 3. Deployment types in depth

There are four deployment types plus (recent) storage classes: SSD, HDD, and Intelligent-Tiering. This section is the reference table.

### 3.1 SCRATCH_1 and SCRATCH_2

- **No replication.** If an OSS or OST fails, files whose stripes intersect that OST become inaccessible; the MDS is not automatically replaced.
- **No backup support** ([Backup support in FSx for Lustre](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-backups-fsx.html#fsxl-backup-support)) — scratch is designed as a *cache* over S3 or short-term compute scratch space.
- **Throughput**: 200 MB/s per TiB baseline. Scratch 2 can **burst to 1300 MB/s per TiB** using a network-credit mechanism; Scratch 1 does not burst.
- **Storage granularity**: SCRATCH_2 provisions in 2.4 TiB increments (2.4, 4.8, 7.2, …), SCRATCH_1 in 3.6 TiB increments. Minimum 1.2 TiB.
- **Encryption at rest**: automatic, using an **AWS-managed** key (customer-managed keys are **not** supported for scratch — see [How Amazon FSx for Lustre uses AWS KMS](https://docs.aws.amazon.com/fsx/latest/LustreGuide/encryption-at-rest.html#FSXKMS)).
- **Encryption in transit**: supported on Scratch 2 (not Scratch 1) from encryption-in-transit-capable EC2 instances.
- **Durability**: probabilistic. AWS publishes:

  | File system size (TiB) | # file servers | Availability/durability over 1 day | over 1 week |
  |---|---|---|---|
  | 1.2 | 2 | 99.9% | 99.4% |
  | 4.8 | 3 | 99.8% | 99.2% |
  | 50.4 | 22 | 99.1% | 93.9% |

  from [Scratch file systems](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html#scratch-file-system). Note weekly durability of a 50 TiB scratch is ~93.9%, meaning **do not use scratch for anything you can't rehydrate**.

### 3.2 PERSISTENT_1

- Supports **SSD** (throughput 50 / 100 / 200 MB/s per TiB) and **HDD** (12 or 40 MB/s per TiB, with an optional SSD read cache sized to 20% of HDD capacity).
- **Data is replicated within the AZ**: each OST is on a set of disks replicated on the storage layer; failed OSSes are replaced within minutes, client I/O retries transparently.
- Backup-eligible; supports customer-managed KMS keys.
- Console lets you create SSD variants; Persistent 1 can only be *created* via CLI/API today, since new deployments are steered to Persistent 2.
- Storage granularity: SSD in 2.4 TiB steps up to 100.8 TiB; HDD in 6 TiB (PERSISTENT-12) or 1.8 TiB (PERSISTENT-40) steps.

### 3.3 PERSISTENT_2 (current default, SSD or Intelligent-Tiering)

The workhorse for modern AI/ML on EKS. Key differences from Persistent 1:

- **Per-unit-storage throughput tiers**: 125, 250, 500, 1000 MB/s per TiB (see per-OSS storage table above).
- **EFA-enabled data path** for aggregate throughputs above 10 GBps, including **GPUDirect Storage** support that lets NVIDIA GPUs DMA directly from FSx over EFA at up to **1200 Gbps per client**. Non-EFA clients cap at 100 Gbps per client, and any *single* client-to-OSS connection is capped at 5 Gbps ([Throughput to individual client instances](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html#throughput-clients)).
- **Independent Metadata IOPS provisioning** (see 2.1).
- SSD variant offers sub-ms latency across the whole dataset; the Intelligent-Tiering storage class (only on P2) fully decouples capacity billing from throughput and auto-tiers cold data.
- Data is replicated within the AZ for SSD; **Intelligent-Tiering P2 is multi-AZ.**

### 3.4 PERSISTENT_2 Intelligent-Tiering

Introduced in 2024. Key properties (from [FSx for Lustre storage classes](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html#lustre-storage-classes) and [How Intelligent-Tiering works](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html#how-INT-tiering-works)):

- Elastic capacity — you pay for what you store, no need to pre-size. Throughput is provisioned in units of 4000 MB/s per OSS, up to 512 TiB per OSS.
- Three access tiers:
  - **Frequent Access** — last 30 days.
  - **Infrequent Access** — 30-90 days idle.
  - **Archive Instant Access** — > 90 days idle.
- Optional SSD read cache for sub-ms latency on hot data.
- Data is **replicated across multiple AZs**, so it survives AZ loss (unlike SSD P2, which is single-AZ).
- All access has the same latency profile *from* the cache; retrievals from IA/AIA have no extra request charge.

### 3.5 Deployment-type feature matrix

| Feature | SCRATCH_1 | SCRATCH_2 | PERSISTENT_1 SSD | PERSISTENT_1 HDD | PERSISTENT_2 SSD | PERSISTENT_2 Intelligent-Tiering |
|---|---|---|---|---|---|---|
| Data replication | No | No | Intra-AZ | Intra-AZ | Intra-AZ | Multi-AZ |
| Backup-eligible | No | No | Yes | Yes | Yes | Yes |
| CMK for at-rest KMS | No | No | Yes | Yes | Yes | Yes |
| In-transit encryption | No | Yes | Yes | Yes | Yes | Yes |
| Burst throughput | No | 1.3 GB/s per TiB | ~1.3 GB/s per TiB | Yes (see HDD table) | No (higher baseline) | N/A (elastic) |
| EFA / GPUDirect | No | No | No | No | Yes (P2 EFA tiers) | No |
| Provisioned Metadata IOPS | No | No | No | No | Yes | Yes |
| Console-createable | Yes | Yes | CLI/API only | CLI/API only | Yes | Yes |

---

## 4. Sizing granularity — the numbers you actually need at `terraform apply` time

Given the "storage per OSS" model, provisioned capacity must be a multiple of the per-OSS granularity, i.e.:

- **SCRATCH_1**: 1200 GiB, then 3600 GiB steps (1200, 2400... actually 1200 then +3600 per additional OSS).
- **SCRATCH_2, Persistent 1 SSD, Persistent 2 non-EFA**: 1200 GiB minimum, **2400 GiB** increments.
- **Persistent 2 EFA (125 MB/s/TiB)**: 38400 GiB per OSS increments (38.4 TiB).
- **Persistent 2 EFA (250 MB/s/TiB)**: 19200 GiB per OSS.
- **Persistent 2 EFA (500 MB/s/TiB)**: 9600 GiB per OSS.
- **Persistent 2 EFA (1000 MB/s/TiB)**: 4800 GiB per OSS.
- **Persistent 1 HDD 12 MB/s/TiB**: 6000 GiB per OSS.
- **Persistent 1 HDD 40 MB/s/TiB**: 1800 GiB per OSS.

Terraform will fail if `storage_capacity` doesn't line up. Example (paraphrased from a real inference-cluster module):

```hcl
resource "aws_fsx_lustre_file_system" "cache" {
  storage_capacity            = 9600            # 9.6 TiB
  storage_type                = "SSD"
  deployment_type             = "PERSISTENT_2"
  per_unit_storage_throughput = 500             # MB/s per TiB → 4.8 GB/s aggregate
  subnet_ids                  = [var.subnet_id]
  security_group_ids          = [aws_security_group.fsx.id]
  kms_key_id                  = aws_kms_key.fsx.arn
  file_system_type_version    = "2.15"          # Lustre 2.15 on Persistent 2

  # Optional S3 linkage
  data_repository_configuration {
    import_path            = "s3://my-bucket/training-data/"
    export_path            = "s3://my-bucket/checkpoints/"
    auto_import_policy     = "NEW_CHANGED"
    imported_file_chunk_size = 1024              # MiB
  }

  automatic_backup_retention_days   = 7
  daily_automatic_backup_start_time = "02:00"
  copy_tags_to_backups              = true

  tags = { Name = "inference-scratch" }
}
```

The Terraform resource reference: [`aws_fsx_lustre_file_system`](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/fsx_lustre_file_system). Note: `per_unit_storage_throughput` is only valid for `PERSISTENT_1` and `PERSISTENT_2`; scratch types omit it.

---

## 5. VPC, ENI, and mount-target model

Unlike EFS, FSx for Lustre does **not** have "mount targets" as a first-class API object. Instead:

1. When you create the file system, you specify **one subnet** (SSD/HDD Persistent) or **multiple subnets** (Intelligent-Tiering P2 for multi-AZ). AWS injects a **primary ENI** — plus one ENI per additional OSS/MDS — into that subnet.
2. The ENIs get RFC 1918 IPs in the subnet's CIDR. The file system's DNS name resolves (**inside the VPC**) to the primary ENI's private IP. You can also enumerate all storage-server IPs from the [`DescribeFileSystems`](https://docs.aws.amazon.com/fsx/latest/APIReference/API_DescribeFileSystems.html) response's `NetworkInterfaceIds`.
3. Clients must be able to open **TCP 988** (Lustre port) to those ENIs. Practically that means a security group that trusts the pod/node security group on 988 ingress.
4. From clients **outside** the file-system VPC (peered VPC, TGW, DX, S2S VPN), you must use the **IP address of the primary ENI**, not the DNS name (DNS resolution is intra-VPC only). See [Mounting from on-premises or peered VPCs](https://docs.aws.amazon.com/fsx/latest/LustreGuide/mounting-on-premises.html). File systems created *after* 2020-12-17 additionally allow non-RFC-1918 clients over peering/TGW/DX/VPN — older ones require RFC 1918 client IPs.

### 5.1 Single-AZ vs multi-AZ

- **SSD Persistent 1/2 and HDD Persistent 1**: single-AZ. If the AZ is impaired, the file system is unavailable until AWS restores it; data is durable (within-AZ replication) but not available. Best practice for critical workloads: cross-AZ backups via AWS Backup or DRT (data repository tasks) to S3.
- **Intelligent-Tiering P2**: multi-AZ. Storage is triplicated across ≥3 AZs; the ENI plane also spans subnets you supply.
- **Scratch (both)**: single-AZ, single-copy. Really is scratch.

Security group example for pod-side access:

```hcl
resource "aws_security_group" "fsx" {
  name        = "fsx-lustre-${random_id.postfix.hex}"
  description = "Lustre client access"
  vpc_id      = var.vpc_id
}

resource "aws_security_group_rule" "fsx_lustre_ingress" {
  security_group_id        = aws_security_group.fsx.id
  type                     = "ingress"
  protocol                 = "tcp"
  from_port                = 988
  to_port                  = 988
  source_security_group_id = var.eks_node_sg_id
}

# Recent AWS docs also recommend opening 1018-1023 to support LNet reconnects.
resource "aws_security_group_rule" "fsx_lnet_reconnect" {
  security_group_id        = aws_security_group.fsx.id
  type                     = "ingress"
  protocol                 = "tcp"
  from_port                = 1018
  to_port                  = 1023
  source_security_group_id = var.eks_node_sg_id
}
```

See [File system access control with Amazon VPC](https://docs.aws.amazon.com/fsx/latest/LustreGuide/limit-access-security-groups.html) for the authoritative list.

---

## 6. Encryption — at-rest and in-transit

### 6.1 At rest

From [Encrypting data at rest](https://docs.aws.amazon.com/fsx/latest/LustreGuide/encryption-at-rest.html):

- Cipher: **XTS-AES-256** block cipher; FIPS 140-2 approved algorithms via the AWS KMS infrastructure.
- Both **file data** and **file-system metadata** are encrypted before being written.
- **Scratch** file systems always use an FSx-managed KMS key that is destroyed with the file system.
- **Persistent** file systems accept an AWS-managed KMS key (`aws/fsx`) or a **customer-managed** symmetric KMS key. Asymmetric KMS keys are rejected.

Required KMS permissions for the file system's service principal on the CMK:

```json
{
  "Sid": "AllowFSxLustreEncryption",
  "Effect": "Allow",
  "Principal": { "Service": "fsx.amazonaws.com" },
  "Action": [
    "kms:Decrypt",
    "kms:GenerateDataKeyWithoutPlaintext",
    "kms:CreateGrant",
    "kms:DescribeKey"
  ],
  "Resource": "*"
}
```

(`kms:Encrypt`, `kms:ReEncrypt*`, `kms:ListAliases` are optional but part of the default key policy.)

### 6.2 In transit

Per [Encrypting data in transit](https://docs.aws.amazon.com/fsx/latest/LustreGuide/encryption-in-transit-fsxl.html):

- Supported on **Scratch 2 and Persistent** file systems (not Scratch 1).
- Encryption is between client and OSS/MDS, **and also for inter-server hops** inside the FSx fabric.
- It only turns on when the client instance type is one of the EC2 types that supports [in-transit encryption](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/data-protection.html#encryption-transit) — Nitro-based instances. Older Xen instances get plaintext-on-wire.
- No client-side toggle; the driver negotiates automatically.
- AWS does not publish the ciphersuite; the practical assumption is AES-GCM keyed via Nitro-attested key material.

Because it depends on the *instance type*, an EKS node group heterogeneous in Nitro vs non-Nitro instances will silently mix cipher states. Modern GPU/inference instances (`g5`, `g6`, `p5`, `inf2`, `trn1/2`) are all Nitro, so this is rarely a concern for inference clusters.

---

## 7. Backup and snapshot semantics

Detailed in [Protecting your data with backups](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-backups-fsx.html):

- **Eligibility**: persistent file systems **not** linked to an S3 data repository. Scratch is ineligible; S3-linked persistent file systems are ineligible because "the S3 bucket serves as the primary data repository and the Lustre file system does not necessarily contain the full dataset at any given time."
- **Nature**: file-system-consistent, block-level, **incremental**. First backup is O(total data), subsequent backups are O(changed blocks). Deleting a backup only removes the blocks unique to it; the underlying S3 store is versioned like an EBS snapshot chain.
- **Durability**: stored in S3 at 11 nines. AWS does not expose the bucket; you interact via the FSx API and AWS Backup.
- **Retention**: automatic daily backups can be retained 0–90 days (default 0, i.e. off). User-initiated and `AWS_BACKUP` type backups persist until you delete them.
- **Restore semantics**: restoring creates a **new file system**. You cannot restore in-place, and the mount name/DNS name will differ.
- **AWS Backup integration**: adds hourly schedules, cross-Region and cross-account copy, unlimited retention, and immutable copies. Backups from AWS Backup show up as `AWS_BACKUP` in the FSx API.

FSx for Lustre does **not** offer point-in-time snapshots the way OpenZFS or Windows FSx do — the atomic unit is a backup.

### 7.1 Data Repository Tasks vs backups

If your persistent file system *is* S3-linked, you use **Data Repository Tasks** (DRTs) instead. See [Data repository tasks](https://docs.aws.amazon.com/fsx/latest/LustreGuide/data-repository-tasks.html):

```bash
aws fsx create-data-repository-task \
  --file-system-id fs-01234567 \
  --type EXPORT_TO_REPOSITORY \
  --paths /fsx/checkpoints/,/fsx/logs/ \
  --report Enabled=true,Path=s3://my-bucket/drt-reports/,Format=REPORT_CSV_20191124,Scope=FAILED_FILES_ONLY
```

An `EXPORT_TO_REPOSITORY` task walks the specified paths and writes changed/new files back to S3; an `IMPORT_METADATA_FROM_REPOSITORY` task refreshes Lustre inodes from a subsequent S3 mutation.

---

## 8. Client protocol — kernel module, mount, and what the pod sees

### 8.1 The Lustre client kernel module

Clients need `lustre.ko`, `lnet.ko`, and their dependent modules loaded. On Amazon Linux 2 / 2023 they are shipped in the base repos; on RHEL / Rocky / Ubuntu, AWS operates signed repositories:

- `https://fsx-lustre-client-repo.s3.amazonaws.com/el/{7,8,9,10}/fsx-lustre-client.repo`
- `https://fsx-lustre-client-repo.s3.amazonaws.com/ubuntu/` (Ubuntu 18.04, 20.04, 22.04, 24.04, both 4KB and 64KB pagesize variants for Graviton)
- `https://fsx-lustre-client-repo.s3.amazonaws.com/suse/sles-12/` (SLES 12 SP3–SP5)

The install commands (from [Installing the Lustre client](https://docs.aws.amazon.com/fsx/latest/LustreGuide/install-lustre-client.html)):

```bash
# Amazon Linux 2023 (kernel ≥ 6.1.79-99.167.amzn2023 or ≥ 6.12*)
sudo dnf install -y lustre-client

# Amazon Linux 2 (kernel ≥ 5.10.144-127.601.amzn2)
sudo amazon-linux-extras install -y lustre

# RHEL / Rocky 9
sudo curl https://fsx-lustre-client-repo.s3.amazonaws.com/el/9/fsx-lustre-client.repo \
     -o /etc/yum.repos.d/aws-fsx.repo
sudo yum install -y kmod-lustre-client lustre-client

# Ubuntu 22.04 (default 4KB page size)
wget -O - https://fsx-lustre-client-repo-public-keys.s3.amazonaws.com/fsx-ubuntu-public-key.asc \
  | gpg --dearmor | sudo tee /usr/share/keyrings/fsx-ubuntu-public-key.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/fsx-ubuntu-public-key.gpg] \
     https://fsx-lustre-client-repo.s3.amazonaws.com/ubuntu $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/fsxlustreclientrepo.list
sudo apt update && sudo apt install -y linux-aws lustre-client-modules-aws
```

Because the module is kernel-locked, kernel updates on the client host need matching Lustre client modules — the `lustre-client-modules-aws` metapackage or `kmod-lustre-client` handles the pairing on `dnf update`.

### 8.2 The mount command

The canonical mount is:

```bash
sudo mount -t lustre \
  fs-01234567.fsx.us-west-2.amazonaws.com@tcp:/fsx1234 \
  /fsx \
  -o defaults,relatime,flock,_netdev
```

Or in `/etc/fstab` (from [Using /etc/fstab to mount FSx for Lustre automatically](https://docs.aws.amazon.com/fsx/latest/LustreGuide/mount-fs-auto-mount-onreboot.html)):

```fstab
# Non-EFA file system
fs-01234567.fsx.us-west-2.amazonaws.com@tcp:/fsx1234 /fsx lustre \
  defaults,relatime,flock,_netdev,x-systemd.automount,x-systemd.requires=network.service 0 0

# EFA-enabled file system
fs-01234567.fsx.us-west-2.amazonaws.com@tcp:/fsx1234 /fsx lustre \
  defaults,relatime,flock,_netdev,x-systemd.automount,\
x-systemd.requires=configure-efa-fsx-lustre-client.service,\
x-systemd.after=configure-efa-fsx-lustre-client.service 0 0
```

The two positional fields decompose as:

- `fs-01234567.fsx.<region>.amazonaws.com@tcp` — the NID list to hit for the MGS/MDS handshake. FSx returns exactly the DNS name; the client resolves it to the primary ENI's IP.
- `:/fsx1234` — the **mount name**. Distinct from the file system ID; must come from `describe-file-systems` output.

Mount-option semantics (see the fstab table in the AWS docs):

| Option | Purpose |
|---|---|
| `defaults` | Default OS mount options — `rw,suid,dev,exec,auto,nouser,async`. |
| `relatime` | Update `atime` only if `atime < mtime` or older than 1 day. Cheaper than default `strictatime`, safer than `noatime` for tools that rely on `atime`. |
| `noatime` | Never update `atime`. Better if you don't care — saves an MDS write per read. |
| `flock` | Enable `flock(2)` and `fcntl(F_SETLK)` **across clients**. Required for sqlite, most databases, jupyter kernels, etc. |
| `noflock` | Disable inter-client flock coordination (advisory only local to the client). |
| `_netdev` | Tell systemd/`mount -a` this is a network device. Prevents the boot from hanging trying to mount before the network is up. |
| `x-systemd.automount` | Convert the fstab entry into a `.automount` unit — the actual mount is triggered on first access, and the OS won't fail early boot if the file system is briefly unreachable. |
| `x-systemd.requires=...` | Order the automount after network / EFA setup services. |
| `nofail` | If mount fails at boot, keep booting. Recommended for any non-essential FSx mount. |

If you omit `_netdev` you can wedge a systemd boot; AWS calls this out with a warning in the fstab docs.

### 8.3 What actually happens on a read/write from an EKS pod

Concrete walkthrough. Setup:

- EKS cluster, `m6i.4xlarge` node, `aws-fsx-csi-driver` v1.x installed.
- StorageClass:

  ```yaml
  apiVersion: storage.k8s.io/v1
  kind: StorageClass
  metadata:
    name: fsx-lustre-p2
  provisioner: fsx.csi.aws.com
  parameters:
    subnetId: subnet-0abc123
    securityGroupIds: sg-0abc123
    deploymentType: PERSISTENT_2
    perUnitStorageThroughput: "500"
    fileSystemTypeVersion: "2.15"
    kmsKeyId: arn:aws:kms:us-west-2:123456789012:key/abcd
    storageType: SSD
  mountOptions:
    - flock
    - noatime
    - _netdev
  reclaimPolicy: Retain
  allowVolumeExpansion: true
  ```

- PVC + Deployment mounting `/fsx` inside the pod.

Sequence:

1. **Pod scheduled** → kubelet calls `NodePublishVolume` on the FSx CSI node plugin (a DaemonSet). The plugin calls FSx to get the DNS name and mount name (or reads them from the PV spec) and invokes:

   ```bash
   mount -t lustre fs-01234567.fsx.us-west-2.amazonaws.com@tcp:/fsx1234 \
     /var/lib/kubelet/plugins/kubernetes.io/csi/pv/pvc-xyz/globalmount \
     -o flock,noatime,_netdev
   ```

   The kernel loads `lustre.ko` (if not already), performs an LNet handshake to the MGS ENI over TCP:988, downloads the file-system layout (OST list, MDT list, quotas), and instantiates a `lustre` superblock.

2. **CSI node plugin bind-mounts** the global mount into the pod's mount namespace: `mount --bind /var/lib/kubelet/plugins/.../globalmount /var/lib/kubelet/pods/<uid>/volumes/kubernetes.io~csi/<pv>/mount`. This means multiple pods on the same host share the same underlying kernel-side Lustre mount, saving a superblock per pod.

3. **Pod `open("/fsx/dataset/img_042.bin")`**:
   - VFS → `lustre` FS → LDLM: acquire "layout intent" lock on the file's inode.
   - `mdc` (client for MDT) sends `MDS_GETATTR_NAME` to the MDS ENI. MDS returns inode metadata + LOV EA (layout).
   - Kernel builds an `osc` client channel to each OSS in the layout.

4. **Pod `read(fd, buf, 4 MiB)`**:
   - LOV maps the byte range to (OST, object_id, offset). If stripe_count=16, a 4 MiB read at offset 0 with 1 MiB stripe size touches 4 different OSTs.
   - Client sends parallel `OST_READ` RPCs to those OSSes; each returns the requested byte range.
   - Client's readahead engine (`lctl set_param llite.*.max_read_ahead_mb`) may prefetch adjacent stripes.
   - Return `read`.

5. **Pod `write(fd, buf, 8 MiB)` + `fsync`**:
   - Data pages are cached in the client's LustreCache, then flushed asynchronously to the OSSes.
   - `fsync` sends `OST_SYNC` to each OST holding a stripe; each OST fsyncs its ldiskfs block layer.
   - `fsync` returns when all OSSes have acknowledged. This is the durability point: once your `fsync` returns 0 on a Persistent P2, your data has been replicated across the OSS's underlying disks.

6. **On pod eviction**, kubelet calls `NodeUnpublishVolume` (unbind); the lustre mount stays live on the host until `NodeUnstageVolume` (the last pod using this PV) triggers `umount`.

The two "gotchas" for EKS:

- **Static pod IPs vs Lustre client eviction.** FSx OSS/MDS use client NIDs derived from the node's ENI IP. If a node's ENI IP changes (rare but possible during ENI recycling), Lustre may evict; typically the client auto-reconnects. Persistent LDLM locks are recoverable within the eviction timeout (~30-60s).
- **Kernel/module version drift.** If a `dnf update` on the node bumps the kernel past the installed `kmod-lustre-client`, the next reboot leaves the node unable to mount. The `installonlypkgs` pinning shown in the AWS install docs is the mitigation. Bottlerocket AMIs solve this by pinning both.

### 8.4 Static provisioning example

Static is often preferred over dynamic in production, so the FS lifecycle isn't tied to K8s reclaim policies:

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: fsx-lustre-static
spec:
  capacity:
    storage: 9600Gi
  volumeMode: Filesystem
  accessModes: [ReadWriteMany]
  mountOptions:
    - flock
    - noatime
    - _netdev
  csi:
    driver: fsx.csi.aws.com
    volumeHandle: fs-01234567
    volumeAttributes:
      dnsname: fs-01234567.fsx.us-west-2.amazonaws.com
      mountname: fsx1234
  persistentVolumeReclaimPolicy: Retain
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: fsx-lustre-static
spec:
  accessModes: [ReadWriteMany]
  storageClassName: ""
  resources:
    requests:
      storage: 9600Gi
  volumeName: fsx-lustre-static
```

`volumeAttributes.dnsname` and `mountname` come straight from `aws fsx describe-file-systems --file-system-ids fs-01234567 --query 'FileSystems[0].{d:DNSName,m:LustreConfiguration.MountName}'`.

---

## 9. Performance — deep dive

### 9.1 Throughput math

For a Persistent 2 SSD file system:

```text
aggregate_baseline_throughput_MBps = capacity_TiB × per_unit_throughput_MBps_per_TiB
aggregate_burst_throughput_MBps    ≤ capacity_TiB × 1300  (only for 125/250 tiers)
disk_throughput_MBps               = capacity_TiB × per_unit_throughput_MBps_per_TiB
network_throughput_MBps            = capacity_TiB × 2.6 × per_unit_throughput  (approx)
```

From [Performance characteristics of SSD and HDD storage classes](https://docs.aws.amazon.com/fsx/latest/LustreGuide/ssd-storage.html):

| Deployment | Network baseline (MBps/TiB) | Network burst | Cache RAM (GiB/TiB) | Disk baseline | Disk burst |
|---|---|---|---|---|---|
| SCRATCH_2 | 200 | 1300 | 6.7 | 200 read / 100 write | — |
| PERSISTENT-125 | 320 | 1300 | 3.4 | 125 | 500 |
| PERSISTENT-250 | 640 | 1300 | 6.8 | 250 | 500 |
| PERSISTENT-500 | 1300 | — | 13.7 | 500 | — |
| PERSISTENT-1000 | 2600 | — | 27.3 | 1000 | — |

Observe the two-level cache: OSS in-memory cache (fed by RAM/TiB in the table) and, for HDD or Intelligent-Tiering, an SSD read cache. Reads that hit RAM are limited by **network** throughput; reads that miss cache are limited by the lesser of network and **disk** throughput.

### 9.2 Client-side throughput

Per-client caps (from [Throughput to individual client instances](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html#throughput-clients)):

| FS type | Client NIC | Max Gbps to file system |
|---|---|---|
| Non-EFA FS | anything | 100 Gbps* |
| EFA FS | ENA | 100 Gbps* |
| EFA FS | ENA Express | 100 Gbps |
| EFA FS | EFA (libfabric) | **700 Gbps** |
| EFA FS | EFA + GDS (GPUDirect Storage) | **1200 Gbps** |

`*` a single client-to-OSS TCP flow is additionally capped at **5 Gbps**. This is why striping across OSTs is what unlocks single-file throughput — otherwise the client is stuck behind one 5 Gbps flow. For a `p5.48xlarge` doing checkpoint saves, you want stripe count 16–32 to saturate the NIC.

### 9.3 Metadata IOPS in practice

The metadata cost table (2 create-open-close per IOPS, 0.1 directory-create per IOPS) implies:

- A shard-per-file training workload (millions of tiny files, many `open`+`stat` per epoch) is metadata-bound. Provision 6000–12000 IOPS minimum.
- Large-checkpoint workloads (a few multi-GB files) barely touch the MDS after the first `open`; you can run with default 1500 IOPS.
- Deep directory hierarchies (`hf-datasets` sharded to depth 4+) are a *directory* workload — those are 0.1 ops/IOPS, i.e. 10x the cost of file ops. Prefer flat layouts.

### 9.4 Perf-tuning knobs on the client

Useful `lctl` and `lfs` handles:

```bash
# Increase readahead per file, per client (default 64 MiB)
lctl set_param llite.*.max_read_ahead_mb=256
lctl set_param llite.*.max_read_ahead_per_file_mb=256

# Increase max_rpcs_in_flight per OSC — helps saturate large aggregate throughput
lctl set_param osc.*.max_rpcs_in_flight=32

# Inspect all OST occupancy from the client
lfs df -h /fsx

# Force a file to stripe across all OSTs (useful for checkpoint files)
lfs migrate --stripe-count -1 --stripe-size 4M /fsx/checkpoints/step-100.pt
```

These knobs need to be re-applied on every mount; put them in a systemd unit that runs after `network.service` and the mount-unit.

---

## 10. Data-repository integration (S3)

FSx for Lustre has a hybrid model where a **Data Repository Association (DRA)** links a Lustre path to an S3 prefix. See [Using data repositories](https://docs.aws.amazon.com/fsx/latest/LustreGuide/fsx-data-repositories.html).

- **Import**: at file-system create time, or via `create-data-repository-association`, FSx walks the S3 prefix and populates Lustre inodes for every object it sees. The *content* is lazily loaded on first read (hydrate-on-read).
- **Auto-import**: `NEW`, `NEW_CHANGED`, or `NEW_CHANGED_DELETED` — controls whether S3 mutations propagate back into the Lustre namespace automatically via EventBridge notifications.
- **Export**: FSx does not push changes back to S3 automatically. You issue an `EXPORT_TO_REPOSITORY` DRT (see §7.1), or use `hsm_release` semantics on newer versions.
- **Chunking**: imported objects > `imported_file_chunk_size` (default 1 GiB) are pre-striped when hydrated.

For inference clusters this is the pattern: warm the FSx file system from S3 model artifacts at cluster spin-up, run training/inference at Lustre throughput, then either `EXPORT_TO_REPOSITORY` the checkpoints or write straight to S3 via a sidecar.

---

## 11. Operational concerns

### 11.1 Capacity expansion

`aws fsx update-file-system --storage-capacity <newCapacity>` is supported on Persistent 1/2 SSD and HDD, subject to:

- New capacity ≥ current + at-least-one-OSS increment (in per-OSS steps).
- Not supported on Scratch, Intelligent-Tiering (which is elastic).
- Expansion also raises throughput proportionally, since MB/s/TiB is fixed.
- Rebalance happens online; existing files are *not* auto-restriped onto the new OSTs, so hot files stay on the original stripe set. Use `lfs migrate` if you need rebalance.

### 11.2 Throughput expansion

For Persistent 2 SSD you can update `per_unit_storage_throughput` (e.g., 250 → 500) via `update-file-system`. Because storage-per-OSS changes with the tier, this triggers an *online restripe* — expect a background workload of hours to days on multi-TB file systems.

### 11.3 Monitoring

FSx emits per-minute CloudWatch metrics per **disk (OST/MDT)** ([Monitoring performance and usage](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html#performance-monitoring)):

| Metric | Meaning |
|---|---|
| `DataReadBytes` / `DataWriteBytes` | Per-OST throughput. Sum across dimensions for aggregate. |
| `MetadataOperations` | Per-MDT ops/s. |
| `FreeDataStorageCapacity` | Per-OST free bytes. |
| `LogicalDiskUsage` | Includes stripe overhead. |
| `FileServerDiskThroughputBalance` | Network-credit balance for burst tiers. |

Grep for hot OSTs: sort `DataReadBytes` sum-per-OST; if one OST is 10x the others, you have a striping problem (usually a single un-striped huge file).

### 11.4 Failure modes and recovery

- **OSS unavailability on Persistent**: within-minutes replacement; clients see brief EIO, then retry succeeds.
- **OSS loss on Scratch**: files with a stripe on that OST return EIO permanently. Peers are still fine.
- **MDS unavailability**: all metadata ops block until failover; existing open file descriptors continue to serve data unless they need a layout refresh.
- **KMS key deletion**: revoking or deleting the CMK renders the file system unusable and unrecoverable — AWS explicitly warns you cannot re-encrypt. Use grants and key rotation, don't delete.
- **Client kernel/module mismatch**: `mount.lustre: mount failed: (0, 0), No such device`. Fix: pin the kernel + `kmod-lustre-client` combo.

---

## 12. Cost model — brief

Pricing is on a per-region basis but the axes are:

- **Storage capacity** (GB-month), varies by storage class (SSD > SSD-with-throughput > HDD > Intelligent-Tiering tiers).
- **Provisioned throughput** (MB/s/TiB × TiB × hour), only on Persistent + on top of storage.
- **Provisioned Metadata IOPS** (Persistent 2 only): above the auto-provisioned baseline, additional IOPS are billed hourly.
- **Backup** (GB-month, incremental).
- **Data-transfer**: cross-AZ egress applies to Lustre traffic; on-VPC-same-AZ is free.
- **Data-Repository requests**: FSx-managed S3 requests during import/export are billed at S3 rates.

See [pricing page](https://aws.amazon.com/fsx/lustre/pricing/) for current per-region rates.

---

## 13. Practical recommendations for EKS inference clusters

1. **Prefer Persistent 2 SSD** at 500 or 1000 MB/s/TiB for GPU inference and training. That gives you the higher per-client throughput ceilings and puts more OSSes behind your data.
2. **Enable EFA on the file system and clients** if you're on H100/H200/B100-class GPUs — 1200 Gbps per client via GDS is not achievable otherwise. Non-EFA caps at 100 Gbps and is *fine* for CPU inference and up to A10G/L40S nodes.
3. **Set striping explicitly** for known-large files (checkpoints, tokenized dataset shards). The default 32-way stripe kicks in only for files > 100 GiB.
4. **Use static provisioning** for the FS itself, dynamic PVs for the mount. This decouples the FS lifecycle from Kubernetes and lets you preserve data through cluster rebuilds.
5. **Pin the Lustre client module** to the running kernel via `installonlypkgs` (Amazon Linux / RHEL) or `lustre-client-modules-aws` (Ubuntu). Bottlerocket AMIs handle this in the image build.
6. **Open the right SG rules**: TCP 988 from the node SG to the FSx SG, plus 1018–1023 for LNet reconnects. Repeat cross-AZ if you use Intelligent-Tiering.
7. **Enable automatic daily backups** with a 7- or 14-day retention on any Persistent file system that isn't S3-linked. Alternatively use AWS Backup for cross-account/cross-region copies.
8. **Prefer S3 linkage for training data**: import cold, hydrate on read. Avoid dumping non-reproducible artefacts in the Lustre FS if you're not backing it up.
9. **Watch `FreeDataStorageCapacity` per OST**, not just total free. Skewed striping shows up here first and produces `ENOSPC` errors even though the FS looks half-empty.
10. **Instrument `flock` usage**. Jupyter, pip, most databases, and many CI runners depend on it. Missing `flock` shows up as `ENOSYS` or hung locks — easy to miss until a real workload runs.

---

## 14. Appendix — quick reference commands

```bash
# List all file systems
aws fsx describe-file-systems

# Get DNS + mount name for a specific FS
aws fsx describe-file-systems --file-system-ids fs-01234567 \
  --query 'FileSystems[0].{d:DNSName,m:LustreConfiguration.MountName}'

# Manual mount
sudo mkdir -p /fsx
sudo mount -t lustre \
  fs-01234567.fsx.us-west-2.amazonaws.com@tcp:/fsx1234 \
  /fsx -o defaults,relatime,flock,_netdev

# Enumerate disks (OSTs + MDT)
lfs df -h /fsx

# Show a file's stripe layout
lfs getstripe /fsx/foo.bin

# Set a directory-wide PFL
lfs setstripe -E 100M -c 1 -E 10G -c 8 -E -1 -c -1 /fsx/big-files

# Restripe an existing file
lfs migrate --stripe-count -1 --stripe-size 4M /fsx/big-files/one-huge-file.bin

# Turn on aggressive readahead client-side
sudo lctl set_param llite.*.max_read_ahead_mb=512
sudo lctl set_param osc.*.max_rpcs_in_flight=32

# Create a user-initiated backup
aws fsx create-backup --file-system-id fs-01234567 --tags Key=Name,Value=pre-migration

# Restore a backup to a new file system
aws fsx create-file-system-from-backup --backup-id backup-01234567 --subnet-ids subnet-abc \
  --security-group-ids sg-abc

# Kick off an S3 export
aws fsx create-data-repository-task \
  --file-system-id fs-01234567 \
  --type EXPORT_TO_REPOSITORY \
  --paths /fsx/checkpoints/ \
  --report Enabled=true,Path=s3://my-bucket/reports/,Format=REPORT_CSV_20191124,Scope=FAILED_AND_SUCCEEDED_FILES
```

---

## 15. References

- [What is Amazon FSx for Lustre?](https://docs.aws.amazon.com/fsx/latest/LustreGuide/what-is.html)
- [Deployment and storage class options](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html)
- [FSx for Lustre performance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html)
- [Performance characteristics of SSD and HDD storage classes](https://docs.aws.amazon.com/fsx/latest/LustreGuide/ssd-storage.html)
- [Performance characteristics of Intelligent-Tiering storage class](https://docs.aws.amazon.com/fsx/latest/LustreGuide/intelligent-tiering-file-systems.html)
- [Installing the Lustre client](https://docs.aws.amazon.com/fsx/latest/LustreGuide/install-lustre-client.html)
- [Mounting your file system automatically with /etc/fstab](https://docs.aws.amazon.com/fsx/latest/LustreGuide/mount-fs-auto-mount-onreboot.html)
- [Encrypting data at rest](https://docs.aws.amazon.com/fsx/latest/LustreGuide/encryption-at-rest.html)
- [Encrypting data in transit](https://docs.aws.amazon.com/fsx/latest/LustreGuide/encryption-in-transit-fsxl.html)
- [Protecting your data with backups](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-backups-fsx.html)
- [Using data repositories](https://docs.aws.amazon.com/fsx/latest/LustreGuide/fsx-data-repositories.html)
- [Amazon FSx for Lustre API Reference](https://docs.aws.amazon.com/fsx/latest/APIReference/Welcome.html)
- [aws-fsx-csi-driver on GitHub](https://github.com/kubernetes-sigs/aws-fsx-csi-driver)
- [FSx for Lustre CSI driver for EKS](https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi.html)
- [Terraform aws_fsx_lustre_file_system resource](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/fsx_lustre_file_system)
- [Lustre.org — upstream documentation](http://lustre.org/)
- [Lustre Operations Manual](https://doc.lustre.org/lustre_manual.xhtml)
- [Managing File Layout (Striping) and Free Space](https://doc.lustre.org/lustre_manual.xhtml#managingstripingfreespace)
