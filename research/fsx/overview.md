---
title: "Amazon FSx family — overview and when to use each"
slug: overview
audience: platform / infra / EKS / ML
scope: entry-point to the FSx research set; routes to deep dives
last_reviewed: 2026-08-06
---

# Amazon FSx family — overview and when to use each

## TL;DR

- Amazon FSx is a **family of four managed file systems**, one per underlying
  open-source or vendor stack: **FSx for Lustre**, **FSx for OpenZFS**,
  **FSx for NetApp ONTAP**, and **FSx for Windows File Server**. They share a
  billing model (per resource, no upfront) and a common set of controls
  (KMS at rest, VPC security groups, IAM, CloudTrail, service-linked role),
  but almost nothing else — protocols, POSIX semantics, deployment shapes,
  and price curves are all different. See
  [What is Amazon FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/what-is.html)
  and the [Amazon FSx product page](https://aws.amazon.com/fsx/).
- If you are running **ML inference or training on EKS** and need shared
  storage for **model weights**, **checkpoints**, or a **hot dataset cache**,
  the answer is almost always **FSx for Lustre**: it is the only flavor that
  striped-parallel-reads a large file across many storage servers, is the
  only one with a first-party integration to hydrate/round-trip from S3 via
  Data Repository Associations, and is the only one that scales to
  GBps-per-client on GPU nodes with EFA. See
  [`ml-inference-patterns.md`](ml-inference-patterns.md) and
  [`lustre-s3-drs.md`](lustre-s3-drs.md).
- If you need **shared POSIX/NFS for Linux and Mac** and the workload looks
  like classic file-server or dev-tooling (small files, snapshots, clones,
  low latency but not GBps aggregate throughput), **FSx for OpenZFS** is the
  right pick — NFSv3/v4/v4.1/v4.2, up to 2M IOPS on Intelligent-Tiering,
  instant snapshots and clones. See
  [FSx for OpenZFS what-is](https://docs.aws.amazon.com/fsx/latest/OpenZFSGuide/what-is-fsx.html).
- If you need **enterprise multi-protocol shared storage** (NFS + SMB + iSCSI
  + NVMe/TCP), **on-prem NetApp parity**, **SnapMirror replication**, WORM
  compliance, or **capacity-pool tiering** to cheap S3-backed storage — use
  **FSx for NetApp ONTAP**. See
  [FSx for ONTAP what-is](https://docs.aws.amazon.com/fsx/latest/ONTAPGuide/what-is-fsx-ontap.html).
- If you need **SMB with Windows ACLs, Active Directory, and native Windows
  semantics** for lift-and-shift of Windows apps, home directories, or
  content management — use **FSx for Windows File Server**. See
  [FSx for Windows what-is](https://docs.aws.amazon.com/fsx/latest/WindowsGuide/what-is.html).
- **Nothing but Lustre** is a strong fit for GPU inference on EKS at
  meaningful scale. OpenZFS/ONTAP can serve a POSIX volume to a Linux pod,
  but a single client is throughput-capped by NFS-over-TCP and by the file
  system's fixed provisioned throughput ceiling; Lustre stripes a single
  file across N OSTs and can put ~1200 Gbps into a single p5 client with EFA.
  For the numbers, see [`cost-perf-benchmarks.md`](cost-perf-benchmarks.md).

---

## 1. What Amazon FSx actually is

"FSx" is the AWS umbrella brand for **fully managed, third-party file
systems** — the AWS pattern of "we run the software, you consume the
protocol." Each family member is a distinct service under the FSx API
namespace (`fsx:CreateFileSystem` with a `FileSystemType` discriminator of
`LUSTRE | OPENZFS | ONTAP | WINDOWS`), with its own dedicated user guide,
IAM actions, CloudWatch namespace, and pricing sheet. They share:

- A common **AWS service-linked role** (`AWSServiceRoleForAmazonFSx`) that
  the service assumes to provision ENIs into your VPC and publish
  CloudWatch metrics.
- A common **network model** — the file system lives in an AWS-managed
  service VPC; ENIs are projected into subnets in *your* VPC, so from your
  side each file system is just a set of ENIs with private IPs, security
  groups, and a DNS name.
- A common **encryption story** — data at rest is XTS-AES-256 with an AWS
  KMS key (AWS-owned by default, customer-managed on request); data in
  transit is provider-specific (Kerberos for SMB, IPsec/TLS/Nitro for ONTAP,
  automatic for Lustre and OpenZFS from Nitro instances).
- A common **API/CLI surface** — `aws fsx create-file-system`,
  `describe-file-systems`, `create-backup`, `create-data-repository-task`
  (Lustre) etc. IAM policies use the `fsx:*` action namespace.
- Common **observability plumbing** — CloudWatch metrics with per-flavor
  dimensions, CloudTrail data events for the control plane, EventBridge
  events for lifecycle transitions.

Everything else — the protocol on the wire, the POSIX guarantees, the
scaling shape, the price per byte and per byte moved — is service-specific.

The FSx family is documented at
[docs.aws.amazon.com/fsx](https://docs.aws.amazon.com/fsx/); each flavor
has its own guide:

| Flavor | User Guide |
|---|---|
| FSx for Lustre | [LustreGuide](https://docs.aws.amazon.com/fsx/latest/LustreGuide/what-is.html) |
| FSx for OpenZFS | [OpenZFSGuide](https://docs.aws.amazon.com/fsx/latest/OpenZFSGuide/what-is-fsx.html) |
| FSx for NetApp ONTAP | [ONTAPGuide](https://docs.aws.amazon.com/fsx/latest/ONTAPGuide/what-is-fsx-ontap.html) |
| FSx for Windows File Server | [WindowsGuide](https://docs.aws.amazon.com/fsx/latest/WindowsGuide/what-is.html) |

### 1.1 What is *not* in the FSx family

A common source of confusion — none of the following are FSx:

- **Amazon EFS** (`elasticfilesystem`, `efs:*`) — AWS's own multi-AZ NFSv4.1
  service. Not part of FSx. Different API. Different price shape (pay per
  GB used, no provisioned throughput required). See
  [EFS docs](https://docs.aws.amazon.com/efs/).
- **Amazon EBS** — block, not file. Attached to a single instance at a
  time (except io2 Multi-Attach for cluster file systems).
- **Amazon S3** — object storage. Presentable as file-ish via
  [Mountpoint for Amazon S3](https://github.com/awslabs/mountpoint-s3),
  but only sequential-read biased and with weak POSIX semantics.
- **File Cache (`AWS File Cache`)** — a distinct service that presents a
  transparent Lustre-based cache in front of on-prem NFS or S3 sources.
  Related tech, but not one of the four FSx flavors. See
  [File Cache docs](https://docs.aws.amazon.com/fsx/latest/FileCacheGuide/what-is.html).

When a stakeholder says "let's use FSx," push back to which of the four —
they behave and cost very differently.

---

## 2. FSx for Lustre

**Use it for**: ML training, ML inference cold start / weight-sharing, HPC,
seismic, genomics, video transcoding, large-scale batch ETL, anything where
aggregate parallel throughput matters more than millisecond-level POSIX
metadata latency.

Full internals are in [`lustre-architecture.md`](lustre-architecture.md).
This section is a routing summary.

### 2.1 What it is

A managed distribution of the open-source [Lustre parallel file
system](http://lustre.org/), currently on the 2.12/2.15 line. AWS runs the
metadata server (MDS/MDT) and object storage server (OSS/OST) fleets in an
AWS-owned service VPC; you consume the file system by mounting `lustre`
over TCP or EFA from your EC2 clients. See
[What is FSx for Lustre](https://docs.aws.amazon.com/fsx/latest/LustreGuide/what-is.html).

The distinguishing property of Lustre is **striping**: a single logical
file's bytes are split across multiple OSTs, so a single reader/writer can
fan out its I/O across many storage servers in parallel. That is what
lets you drive multi-GBps into a single client — you cannot do that on
NFS-over-TCP without protocol extensions.

### 2.2 Protocol

**Lustre**, POSIX-compliant. Not NFS, not SMB. The client is a Linux kernel
module (`lustre.ko`) plus a userspace toolkit (`lfs`, `lctl`). AWS ships a
maintained fork of the client via `amzn-linux-extras` on Amazon Linux 2 and
built-in in Amazon Linux 2023; RHEL/Ubuntu/SUSE builds are in the
[fsx-lustre-client-repo](https://docs.aws.amazon.com/fsx/latest/LustreGuide/install-lustre-client.html)
package feed.

Mount syntax:

```bash
sudo mount -t lustre -o defaults,noatime,flock \
  fs-01234567.fsx.us-east-1.amazonaws.com@tcp:/mountname \
  /mnt/fsx
```

For the EKS mount path, see [`eks-csi-driver.md`](eks-csi-driver.md).

### 2.3 Deployment types

Four generations, all live in the API, three still recommended for new
work:

| Deployment | State | Storage | Notes |
|---|---|---|---|
| `SCRATCH_1` | legacy | SSD | Not recommended for new work; single AZ; no replication. |
| `SCRATCH_2` | current | SSD | Encryption in transit on supported instance types; 200 MB/s per TiB baseline, 1300 MB/s per TiB burst. Cheapest tier, no replication, no backups. |
| `PERSISTENT_1` | legacy | SSD or HDD | 50/100/200 MB/s per TiB (SSD); 12/40 MB/s per TiB (HDD). API/CLI only for new file systems. |
| `PERSISTENT_2` | current | SSD | 125/250/500/1000 MB/s per TiB, optionally EFA-enabled data path (`EFA` deployment sub-type). Replicated within AZ, block-level backups to S3. |
| `PERSISTENT_2` (Intelligent-Tiering) | current | Elastic Frequent/Infrequent/Archive tiers, optional SSD read cache | Pay per byte stored per tier + throughput + IOPS. Scales up to multi-TBps. |

See
[Deployment and storage class options](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html)
and the [Lustre architecture note](lustre-architecture.md) for a full
breakdown of storage-per-OSS, striping, and per-tier caps.

### 2.4 Throughput and IOPS

- **Aggregate throughput** = `capacity_TiB × per_unit_throughput_MBps_per_TiB`.
  4.8 TiB × 1000 MB/s/TiB = **4.8 GB/s baseline** for the whole file system.
  Add **burst credits** for SCRATCH_2 (up to 1300 MB/s/TiB, budget-limited).
- **Per-client cap**: a single non-EFA client tops out at ~100 Gbps. On EFA
  it climbs to **~1200 Gbps** per p5 client. Any single client↔OSS pair is
  capped at 5 Gbps — you need striping and multiple OSSes to saturate a
  fat file system. See
  [Throughput to individual client instances](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html#throughput-clients).
- **Metadata IOPS**: independently provisionable on PERSISTENT_2 SSD (DNE)
  from 1500 to 192000 IOPS. Automatic mode scales with capacity. See
  [File system metadata performance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html#dne-metadata-performance).
- **Latency**: sub-millisecond for SSD tiers; single-digit ms for
  cache-miss reads from HDD/Intelligent-Tiering.

### 2.5 Price shape

You pay for **storage GB-month** (per storage class), **provisioned
throughput MBps-month** (or included with capacity, depending on tier),
**Metadata IOPS-month** (above default), **backup GB-month**, and
Intelligent-Tiering **request counts**. Cross-AZ data in and out of the file
system is charged at $0.01/GB each direction. See
[FSx for Lustre pricing](https://aws.amazon.com/fsx/lustre/pricing/).

For worked cost/perf examples on realistic model weights, see
[`cost-perf-benchmarks.md`](cost-perf-benchmarks.md).

### 2.6 Deployment models and availability

- **Single-AZ only** — all deployment types (Scratch and Persistent) live
  in a **single AZ**. SCRATCH_* has no replication and no backups;
  PERSISTENT_* replicates data within the AZ and supports backups. If the
  AZ fails, the file system is unavailable.
- **DR pattern**: back the file system with a Data Repository Association
  to an S3 bucket that itself has Cross-Region Replication. Data survives;
  the file system does not. See [`lustre-s3-drs.md`](lustre-s3-drs.md).
- **Backups**: block-level incremental to S3-backed managed store at
  11 nines durability. Restore is to a **new** file system, not in-place.
  Only PERSISTENT (and only when not linked to a DRA) supports backups —
  the assumption is that S3 already is your backup.

### 2.7 Encryption

- **At rest**: XTS-AES-256, KMS-backed. Customer-managed KMS keys supported
  on PERSISTENT_* (Scratch uses AWS-managed keys only).
- **In transit**: automatic on SCRATCH_2 and PERSISTENT_* file systems from
  supported EC2 instance types. See
  [Data encryption in FSx for Lustre](https://docs.aws.amazon.com/fsx/latest/LustreGuide/encryption-fsxl.html).

### 2.8 Why it's the pick for ML on EKS

Answered in depth in [`ml-inference-patterns.md`](ml-inference-patterns.md).
Short version:

- **Striping** — a single 140 GB Llama-3-70B weight file spreads across all
  OSTs and multiple clients can read it in parallel; every other FSx flavor
  serves the file through a single logical head.
- **S3 integration via DRAs** — you keep the source of truth in S3 and
  hydrate on demand, with `lazy_load` + `preload` semantics and
  bidirectional export back to S3 (see
  [`lustre-s3-drs.md`](lustre-s3-drs.md)).
- **EFA fast path** — on p5/p5e/p5en/g6e with EFA-enabled deployment,
  single-client bandwidth is far above what NFS-over-TCP can do.
- **CSI driver first-class** — the
  [`aws-fsx-csi-driver`](https://github.com/kubernetes-sigs/aws-fsx-csi-driver)
  supports both static PV binding (hydrate once, mount everywhere) and
  dynamic provisioning with DRAs baked into the StorageClass. See
  [`eks-csi-driver.md`](eks-csi-driver.md).

---

## 3. FSx for OpenZFS

**Use it for**: shared NFS on Linux/macOS where you want ZFS features
(instant snapshots, clones, deduplication, compression), submillisecond
latency, and up to 2M IOPS — but you don't need multi-protocol,
enterprise-grade replication, or GBps-per-client parallel throughput.

### 3.1 What it is

A managed distribution of the [OpenZFS](https://openzfs.github.io/openzfs-docs/)
file system, running on the AWS Nitro system with AWS Scalable Reliable
Datagram (SRD) networking. Presents shared file storage over NFS. See
[What is FSx for OpenZFS](https://docs.aws.amazon.com/fsx/latest/OpenZFSGuide/what-is-fsx.html).

The pitch is "on-prem-parity ZFS with fully managed cloud economics" — you
get pool → dataset → volume → snapshot semantics familiar from any FreeBSD
or Illumos shop, but without patching zfsonlinux or managing zpool health
yourself.

### 3.2 Protocol

- **NFS v3, v4.0, v4.1, v4.2** — all four versions supported. No SMB. No
  iSCSI. No Lustre.
- Native Unix (POSIX) permissions; **no** Windows ACLs.
- IPv4-only or dual-stack (IPv4+IPv6) network types, switchable at any
  time.

### 3.3 Deployment types

- **Multi-AZ (HA)** — primary + standby in different AZs, ~60s failover.
- **Single-AZ (HA)** — primary + standby in the same AZ, ~60s failover.
- **Single-AZ (non-HA)** — no standby; self-healing on component failure,
  ~30 minute recovery window.

See
[Availability and durability for FSx for OpenZFS](https://docs.aws.amazon.com/fsx/latest/OpenZFSGuide/availability-durability.html).

### 3.4 Storage classes

Two, both live in the API:

- **SSD** — pay for provisioned GB; low latency across the full dataset.
- **Intelligent-Tiering** — fully elastic, tiered across Frequent /
  Infrequent / Archive Instant Access; optional SSD read cache. Suitable
  for most workloads; only cost you pay for is bytes you store plus
  requests.

### 3.5 Throughput and IOPS

- Up to **21 GBps** of throughput for cache-hot data.
- Up to **400,000 IOPS to disk**, up to **2M IOPS from cache**.
- Latencies of a few hundred microseconds from cache, submillisecond from
  SSD.
- All numbers are per-file-system aggregates; a single NFS client's
  throughput is bounded by its NIC and by the NFSv4 read/write path (much
  lower than the aggregate).

See [Performance for FSx for OpenZFS](https://docs.aws.amazon.com/fsx/latest/OpenZFSGuide/performance.html).

### 3.6 Price shape

- SSD: **storage GB-month** + **throughput MBps-month** + **provisioned
  IOPS**.
- Intelligent-Tiering: per-tier GB-month + read/write requests + optional
  SSD cache GB-month.
- Backups (per GB-month), cross-region copy per GB, cross-AZ data transfer
  per GB (Multi-AZ only).

See [FSx for OpenZFS pricing](https://aws.amazon.com/fsx/openzfs/pricing/).

### 3.7 Data management features

The reason to pick OpenZFS over the cheaper EFS is these primitives:

- **Instant point-in-time snapshots**, up to tens of thousands per file
  system, near-zero-cost as copy-on-write.
- **Cloning**: create a writable clone of a snapshot in seconds — extremely
  useful for CI/CD, dev environments, or per-request sandboxing.
- **Inline compression** and **deduplication**.
- **Volumes with quotas** — thin-provisioned, per-team/per-user isolation.
- **Amazon S3 Access Points on top of an OpenZFS volume** — you can present
  the volume through an S3 API for compatibility. See
  [S3 Access Points for FSx](https://docs.aws.amazon.com/fsx/latest/OpenZFSGuide/s3accesspoints-for-FSx.html).

### 3.8 Encryption and availability

- **At rest**: KMS-managed, customer-managed key supported.
- **In transit**: automatic from Nitro EC2 instances; NFS Kerberos not
  supported at the time of writing (check the [in-transit encryption
  docs](https://docs.aws.amazon.com/fsx/latest/OpenZFSGuide/data-protection.html)).
- **Availability**: Multi-AZ variant offers cross-AZ redundancy in the
  region.

### 3.9 Fit for ML on EKS

Marginal. It presents NFS, so a pod can mount it, but you lose Lustre's
per-file striping. Reasonable for **artifact/checkpoint sharing** in
smaller-scale training where you want ZFS clones as a fast per-experiment
sandbox — but for hot dataset cache and model weights, Lustre wins.

---

## 4. FSx for NetApp ONTAP

**Use it for**: enterprise storage workloads that need **multi-protocol**
access (Linux + Windows + block), operational parity with on-premises
NetApp, SnapMirror-based DR, WORM compliance, and capacity-pool tiering.
Also: any application that already speaks ONTAP CLI/REST and where you
don't want to re-tool.

### 4.1 What it is

Fully managed [NetApp ONTAP](https://www.netapp.com/data-management/ontap-data-management-software/),
one of the most feature-rich enterprise file systems in existence. You get
storage virtual machines (SVMs), FlexVols, snapshots, SnapMirror
replication, deduplication, compression, compaction, and antivirus
scanning, all controllable through the NetApp CLI/REST API in addition to
the AWS API. See
[What is FSx for ONTAP](https://docs.aws.amazon.com/fsx/latest/ONTAPGuide/what-is-fsx-ontap.html).

### 4.2 Protocols

The differentiator: **all four** of the enterprise storage protocols in a
single file system.

- **NFS v3, v4, v4.1, v4.2**
- **SMB 2.x/3.x** with **Active Directory + Windows ACLs**
- **iSCSI** — block LUNs
- **NVMe over TCP** — block LUNs at NVMe latencies

That combination is unique among the FSx family (and, arguably, in AWS
overall).

### 4.3 Deployment types

- **Multi-AZ** — HA pair spanning two AZs; automatic failover between the
  two nodes.
- **Single-AZ** — HA pair within a single AZ; lower cross-AZ data transfer
  cost.

See
[Availability and durability](https://docs.aws.amazon.com/fsx/latest/ONTAPGuide/high-availability-AZ.html).

### 4.4 Storage tiers

- **SSD** — provisioned per volume; submillisecond latency.
- **Capacity pool** — elastic, S3-backed, pay per GB used + requests.
  Automatic policy-driven tiering moves cold blocks between SSD and
  capacity pool.

You get "SSD level performance while paying for SSD storage for only a
small fraction of your data" — the value prop the docs lead with.

### 4.5 Throughput and IOPS

- **Throughput capacity** provisioned per file system in MBps units, from
  128 MBps up to tens of GBps depending on generation and region.
- **SSD IOPS** provisioned per volume; scale independently of capacity.
- **Latencies**: submillisecond for SSD-resident data.

See [FSx for ONTAP performance](https://docs.aws.amazon.com/fsx/latest/ONTAPGuide/performance.html).

### 4.6 Price shape

Per the docs, billing categories are:

- SSD storage capacity (GB-month)
- SSD IOPS provisioned above 3 IOPS/GB (IOPS-month)
- Throughput capacity (MBps-month)
- Capacity pool storage consumption (GB-month)
- Capacity pool requests (per R/W)
- Backup storage consumption (GB-month)

See [FSx for ONTAP pricing](https://aws.amazon.com/fsx/netapp-ontap/pricing/).
The number of levers is high — model it carefully before you commit.

### 4.7 Data management features

ONTAP's calling card is the tooling depth:

- **SnapMirror**: block-level asynchronous replication to another ONTAP
  system (on-prem or in-cloud, cross-region). This is the enterprise DR
  primitive.
- **FlexCache**: caching front-end for hybrid deployments.
- **SnapLock**: WORM with Compliance and Enterprise retention modes for
  regulated workloads.
- **VSS** integration and **antivirus scanning** hooks.
- Native **NetApp Data Infrastructure Insights** and **Harvest**
  observability integrations.

### 4.8 Encryption and compliance

- **At rest**: KMS-backed.
- **In transit**: SMB Kerberos, IPsec (for NFS), Nitro-based encryption
  between EC2 and the file system. See
  [Encryption in transit](https://docs.aws.amazon.com/fsx/latest/ONTAPGuide/encryption-in-transit.html).
- **Compliance**: ISO, PCI-DSS, SOC, HIPAA eligible.

### 4.9 Fit for ML on EKS

Uncommon. ONTAP can serve NFS to an EKS pod without issue, and its
multi-protocol nature can be attractive when the same dataset must also be
consumed by a Windows visualization pipeline. But for pure ML feed the cost
per byte and per byte moved is high vs. Lustre, and the parallel-throughput
ceiling is lower per single client. If the shop already uses ONTAP on prem
and wants zero data-management retraining, this can be the right compromise.

---

## 5. FSx for Windows File Server

**Use it for**: Windows-native shared storage. Home directories, roaming
profiles, Windows dev shares, SQL Server FileStreams, Windows CMS/DAM,
video editorial. Anything where the client is Windows, the auth model is
Active Directory, and the ACLs matter.

### 5.1 What it is

A managed **native Windows Server** file server. Not Samba on Linux —
actual Windows Server with a Windows file system underneath. Native SMB,
native NTFS ACLs, native VSS, native DFS Namespaces. See
[What is FSx for Windows File Server](https://docs.aws.amazon.com/fsx/latest/WindowsGuide/what-is.html).

### 5.2 Protocol

- **SMB 2.0 through 3.1.1**. No NFS. No Lustre. No iSCSI.
- Windows ACLs (NTFS-style), Windows shadow copies, DFS Namespaces.
- Access from Windows 7+, Windows Server 2008+, and current Linux distros
  via `cifs-utils` / `mount.cifs`.
- **Active Directory required** — either AWS Managed Microsoft AD or your
  self-managed AD via trust.

### 5.3 Deployment types

- **Single-AZ 1** — SSD only, legacy.
- **Single-AZ 2** — SSD or HDD, current.
- **Multi-AZ** — active/standby across two AZs; ~30s failover.

See
[Availability and durability](https://docs.aws.amazon.com/fsx/latest/WindowsGuide/high-availability-multiAZ.html).

### 5.4 Storage classes

- **SSD** — submillisecond, high IOPS. For databases, media processing,
  analytics.
- **HDD** — cheaper, higher latency; suits home directories, general
  shares, CMS.

You provision storage GB, SSD IOPS, and throughput capacity **independently**.

### 5.5 Throughput and IOPS

- Throughput capacity 8 to 4096 MBps per file system, scalable in-place.
- SSD IOPS from 3 per GB up to 350,000 (regional-dependent).
- Latencies: submillisecond on SSD, single-digit ms on HDD.

See [FSx for Windows performance](https://docs.aws.amazon.com/fsx/latest/WindowsGuide/performance.html).

### 5.6 Price shape

- Storage GB-month (SSD or HDD).
- SSD IOPS-month above default.
- Throughput capacity MBps-month.
- Backup storage GB-month.
- Cross-AZ data transfer per GB on Multi-AZ.

See [FSx for Windows pricing](https://aws.amazon.com/fsx/windows/pricing/).

### 5.7 Encryption and integration

- **At rest**: KMS-backed.
- **In transit**: automatic SMB Kerberos session keys.
- Deep integration with **AWS Managed Microsoft AD**, **Amazon
  WorkSpaces**, **AppStream 2.0**, **VMware Cloud on AWS**.

### 5.8 Fit for ML on EKS

Poor. Windows semantics on a Linux GPU node is a mount-and-hope exercise —
technically possible via `cifs`, practically painful. If your inference
workload needs to share files with a Windows-based upstream, put the
Windows side in front of an S3 bucket or an ONTAP SVM rather than mounting
FSx for Windows into pods.

---

## 6. Decision matrix

Use this as a triage grid, not gospel. See the deep-dive notes for the
"why" behind each answer.

### 6.1 By protocol

| You need... | Pick |
|---|---|
| Lustre (POSIX, parallel, striped) | **FSx for Lustre** |
| NFS (v3, v4.0, v4.1, v4.2) only | **FSx for OpenZFS** |
| SMB only, Windows-native, Windows ACLs, AD | **FSx for Windows File Server** |
| NFS + SMB + iSCSI + NVMe/TCP in one filesystem | **FSx for NetApp ONTAP** |
| Object (S3 API) with file-like access | **Amazon S3 + Mountpoint** (not FSx) |
| Simple regional NFS with pay-per-use, no throughput to manage | **Amazon EFS** (not FSx) |

### 6.2 By workload shape

| Workload | Pick | Why |
|---|---|---|
| Model weights / checkpoints / dataset cache for GPU inference on EKS | **FSx for Lustre** | Striping, EFA path, S3 DRA, CSI driver. See [`ml-inference-patterns.md`](ml-inference-patterns.md). |
| Distributed model training, hundreds of GPUs, TB checkpoints | **FSx for Lustre PERSISTENT_2 EFA** or **Intelligent-Tiering** | Aggregate throughput and single-client bandwidth. |
| Genomics / seismic / financial modeling / VFX render farm | **FSx for Lustre** | Throughput-bound HPC. |
| Home directories, roaming profiles, Windows dev shares | **FSx for Windows** | SMB + AD + ACLs. |
| SQL Server / Windows enterprise app storage | **FSx for Windows** or **FSx for ONTAP (SMB SVM)** | Depends on need for multi-protocol. |
| SAP HANA storage layer | **FSx for ONTAP** | Multi-protocol + snapshots + performance. |
| Oracle / IBM DB2 / VMware Cloud storage | **FSx for ONTAP** | ONTAP compatibility. |
| Lift-and-shift from on-prem NetApp | **FSx for ONTAP** | Same tooling, SnapMirror bridging. |
| Lift-and-shift from on-prem ZFS / Solaris / FreeBSD | **FSx for OpenZFS** | ZFS parity. |
| Shared dev / build cache for Linux CI | **FSx for OpenZFS** or **EFS** | NFS, snapshots (OpenZFS) or pay-per-use (EFS). |
| Data lake / ML training on frozen dataset | **S3 + Lustre DRA** | Cheap storage in S3, hot cache in Lustre. |
| Regulated WORM records (SEC 17a-4, FINRA) | **FSx for ONTAP (SnapLock)** | Only FSx flavor with WORM. |
| Cross-region DR for a file system | **FSx for ONTAP (SnapMirror)** or **Lustre + S3 CRR** | ONTAP has native cross-region replication; Lustre uses S3 as the transport. |

### 6.3 By availability requirement

| Requirement | Pick |
|---|---|
| Multi-AZ file system, transparent to clients | **OpenZFS Multi-AZ**, **ONTAP Multi-AZ**, or **Windows Multi-AZ** |
| Single-AZ, cheapest, tolerate loss on AZ failure | **Lustre SCRATCH_2** |
| Single-AZ, replicated inside AZ, backed up | **Lustre PERSISTENT_2**, **OpenZFS Single-AZ (HA)**, **ONTAP Single-AZ**, **Windows Single-AZ 2** |
| Cross-region DR | **ONTAP SnapMirror** or **Lustre + S3 CRR** |

### 6.4 By $/GB shape

Rough ranking (us-east-1, SSD equivalents, mid-2026, always verify against
current pricing pages):

1. **Cheapest per stored GB**: **FSx for Lustre SCRATCH_2** — no
   replication, no backups. Not for durable data.
2. **Cheapest tiered**: **Lustre Intelligent-Tiering** and **OpenZFS
   Intelligent-Tiering** — pay per byte per tier, cold data drops to
   $0.004–0.0125/GB-month.
3. **Mid**: **FSx for Windows HDD**, **FSx for ONTAP capacity pool**.
4. **Higher**: **FSx for Windows SSD**, **FSx for OpenZFS SSD**, **FSx for
   Lustre PERSISTENT_2** (throughput tier dominates).
5. **Highest per stored GB**: **FSx for ONTAP SSD with high provisioned
   throughput and IOPS** — enterprise features cost enterprise money.

For real numbers, see [`cost-perf-benchmarks.md`](cost-perf-benchmarks.md).

### 6.5 The "just pick one" flowchart

```
Is the workload Windows or does it need SMB+AD+NTFS ACLs?
├── Yes ──→ FSx for Windows (or FSx for ONTAP if you also need NFS)
└── No
    │
    Does a single logical file need to be read/written at multi-GB/s aggregate,
    or do you need to hydrate from S3 into a POSIX namespace?
    ├── Yes ──→ FSx for Lustre
    └── No
        │
        Do you need multi-protocol (NFS + SMB + iSCSI + NVMe/TCP), enterprise
        replication, or NetApp on-prem compatibility?
        ├── Yes ──→ FSx for NetApp ONTAP
        └── No
            │
            Do you need instant snapshots/clones, high per-client IOPS,
            standard NFS on Linux/Mac?
            ├── Yes ──→ FSx for OpenZFS
            └── No ──→ You probably want Amazon EFS (not FSx). See
                       https://docs.aws.amazon.com/efs/
```

---

## 7. Why FSx for Lustre is the pick for ML/inference on EKS

This is a research repo about EKS for inference workloads
([template code](../../libs/inference-tf-aws-eks-karpenter)). The short
case for Lustre on this stack:

### 7.1 The workload

Pods on GPU nodes (`p5.*`, `p4d.24xlarge`, `g6e.*`, `trn1.*`) cold-starting
a Deployment, each needing to hydrate **tens to hundreds of GB of model
weights** into GPU HBM before serving a request. Scale-out events can drop
a dozen nodes at once. The dominant cost of "time to first token" for a
new replica is the storage read.

### 7.2 Why Lustre wins on that workload

1. **Aggregate throughput is bought, not begged**. A 4.8 TiB
   PERSISTENT_2 SSD at 1000 MB/s per TiB gives you **4.8 GB/s baseline**
   across the whole file system, provisioned deterministically. NFS-based
   flavors give you a single provisioned-throughput knob (in MBps) that
   plateaus far lower.
2. **Single-file striping**. A 140 GB Llama-3-70B `.safetensors` file
   striped across 16 OSTs is read in parallel by a single client — Lustre
   is *the* file system where one file is a parallel object. NFS reads a
   file through one head.
3. **Per-client bandwidth scales with the NIC**. On a p5 with EFA, a single
   client can pull ~1200 Gbps from the file system if the file is striped
   enough. On NFS you're bounded by TCP window/head throughput.
4. **S3 is the source of truth, Lustre is the cache**. Data Repository
   Associations import S3 object listings as inodes, and reads are
   lazy-loaded on first access. You keep weights in a cheap versioned S3
   bucket; Lustre is a warm cache in front. See
   [`lustre-s3-drs.md`](lustre-s3-drs.md).
5. **Kubernetes CSI is first-class**. The
   [aws-fsx-csi-driver](https://github.com/kubernetes-sigs/aws-fsx-csi-driver)
   handles both static PV binding (pre-hydrated FS, mount into many pods)
   and dynamic provisioning; DRAs can be set via `parameters` on the
   StorageClass. Details in [`eks-csi-driver.md`](eks-csi-driver.md).
6. **Metadata scales independently**. On a 500k-file dataset, DNE-provisioned
   metadata IOPS let you separate namespace throughput from data
   throughput. Untarring a HuggingFace model of thousands of shards no
   longer serializes on a single MDS.

### 7.3 What Lustre is *not* good at

Balancing the pitch:

- **Multi-AZ availability**. Every FSx for Lustre file system is single-AZ.
  Your fleet needs to either (a) not care about AZ failures because S3
  holds truth and you re-hydrate elsewhere, or (b) run parallel file
  systems in multiple AZs with an S3 DRA under each. See
  [`gotchas-limits.md`](gotchas-limits.md).
- **Windows / macOS clients**. Not supported; the Lustre client is Linux
  only.
- **Small-file, metadata-heavy random workloads**. Lustre handles them, but
  OpenZFS-on-SSD or ONTAP are lower-latency for classic file-server churn.
- **Sub-GB checkpoints being written every second**. If you're writing a
  ton of small files in parallel from thousands of ranks, watch the
  metadata IOPS provisioning; a scratch FS can bottleneck on `open` calls.
- **Backup story**. PERSISTENT-only, and mutually exclusive with DRAs. In
  practice DRAs + S3 versioning is your backup.

For the depth on all of these, see the sibling notes:

- [`lustre-architecture.md`](lustre-architecture.md) — the file system
  itself
- [`lustre-s3-drs.md`](lustre-s3-drs.md) — hydration and export
- [`eks-csi-driver.md`](eks-csi-driver.md) — mounting from pods
- [`ml-inference-patterns.md`](ml-inference-patterns.md) — sizing,
  striping, hydration order
- [`networking-security.md`](networking-security.md) — VPC/SG/KMS/pod
  identity
- [`cost-perf-benchmarks.md`](cost-perf-benchmarks.md) — $/GB, MBps math
- [`terraform-eks-integration.md`](terraform-eks-integration.md) —
  HCL for the whole stack
- [`gotchas-limits.md`](gotchas-limits.md) — the edges

---

## 8. Cross-cutting concerns

### 8.1 Encryption model, unified view

All four flavors encrypt at rest by default using an AWS KMS key. The
knobs differ:

| Flavor | AWS-managed KMS | Customer-managed KMS | In-transit |
|---|---|---|---|
| Lustre SCRATCH_* | yes | no | automatic on SCRATCH_2 from supported instances |
| Lustre PERSISTENT_* | yes | yes | automatic from supported instances |
| OpenZFS | yes | yes | automatic from Nitro instances |
| ONTAP | yes | yes | SMB Kerberos + IPsec + Nitro |
| Windows | yes | yes | automatic SMB Kerberos session keys |

For KMS setup and pod-identity IAM patterns, see
[`networking-security.md`](networking-security.md).

### 8.2 Backup story

Every flavor supports FSx-managed backups except Lustre SCRATCH_* and
Lustre PERSISTENT with a DRA linked. In those two cases the source of
truth is elsewhere (either "nowhere, it's scratch" or "S3, use versioning
+ CRR").

| Flavor | Automatic daily backup | Manual backup | Cross-region copy |
|---|---|---|---|
| Lustre SCRATCH_* | no | no | via S3 DRA export |
| Lustre PERSISTENT_* (no DRA) | yes | yes | yes |
| Lustre PERSISTENT_* (with DRA) | no | no | via S3 DRA export |
| OpenZFS | yes | yes | yes |
| ONTAP | yes | yes | yes (backups **and** SnapMirror) |
| Windows | yes | yes | yes |

### 8.3 Networking

All four project ENIs into your VPC. You choose subnet(s):

- **Single-AZ** file systems: 1 subnet.
- **Multi-AZ / HA** file systems: 2 subnets in 2 AZs (preferred + standby).

Security group rules are protocol-specific:

- Lustre: TCP 988 + 1018–1023 (LNet ports).
- NFS (OpenZFS, ONTAP-NFS): TCP/UDP 111, 2049, 20001–20003 (mount, nlm,
  quota).
- SMB (Windows, ONTAP-SMB): TCP 445; plus 88, 389, 464 for Kerberos/AD.
- iSCSI/NVMe/TCP (ONTAP): TCP 3260 and 4420.

For the exact set and worked security group examples, see
[`networking-security.md`](networking-security.md).

### 8.4 IAM and pod identity

All four use the service-linked role `AWSServiceRoleForAmazonFSx` for
control-plane operations. From EKS, the interesting IAM is at the CSI
driver level: the driver's controller pod needs `fsx:CreateFileSystem`,
`fsx:DeleteFileSystem`, `fsx:DescribeFileSystems`, etc. See
[`eks-csi-driver.md`](eks-csi-driver.md) and
[`networking-security.md`](networking-security.md) for both IRSA and Pod
Identity patterns.

### 8.5 Observability

CloudWatch namespaces and dimensions:

- `AWS/FSx` — common namespace, dimensions differ per flavor.
- Lustre exposes `DataReadBytes`, `DataWriteBytes`,
  `MetadataOperations`, `FreeDataStorageCapacity`, and Intelligent-Tiering
  specific metrics like `IntelligentTieringStorageUsed`.
- ONTAP exposes SVM-level and volume-level metrics; also NetApp EMS
  events for the ONTAP internals.
- OpenZFS exposes per-volume storage and throughput metrics.
- Windows exposes native SMB and NTFS-style metrics.

Everything CloudTrails; API-level auditing is uniform across the family.

---

## 9. Anti-patterns and gotchas across the family

For the full list see [`gotchas-limits.md`](gotchas-limits.md). Family-wide
highlights:

- **Confusing FSx with EFS.** They serve overlapping use cases but have
  entirely different price models, quotas, and API namespaces. If someone
  says "let's put it on FSx" and the workload is "one team's shared Linux
  home dirs at 100 GB total," it might really want to be EFS.
- **Picking Lustre when you don't need parallel throughput.** Lustre is
  cost-effective only when the workload can exercise the throughput you
  are provisioning. A 1.2 TiB file system doing 5 MB/s of read is wasted.
- **Picking ONTAP when you don't need multi-protocol.** ONTAP has the
  richest management model in the family; that comes with the highest
  operational surface and price.
- **Not planning for AZ failure.** All Lustre and some OpenZFS deployment
  types are single-AZ. Multi-AZ availability requires either the Multi-AZ
  variants (OpenZFS/ONTAP/Windows) or an application-level strategy
  (Lustre + S3 CRR).
- **Ignoring cross-AZ data transfer costs.** A pod in AZ-A reading from a
  file system in AZ-B pays $0.01/GB each direction. Always co-locate.
- **Assuming backups behave like snapshots.** FSx-managed backups restore
  to a *new* file system with a new DNS name — not in place, not
  incremental to the same volume. Plan mount-time DNS rebinding.
- **KMS key lifecycle.** If you use a customer-managed KMS key and delete
  it (or lose permissions), the file system becomes unreadable. Grant the
  FSx service-linked role decrypt permission and keep the key alive.

---

## 10. Where to go next

You almost always want one of the following after this note:

| If you want... | Read |
|---|---|
| The Lustre parallel FS mental model (MDS/OSS/OST, striping, DNE) | [`lustre-architecture.md`](lustre-architecture.md) |
| How Lustre and S3 stay in sync — DRAs, lazy loading, export | [`lustre-s3-drs.md`](lustre-s3-drs.md) |
| Mounting FSx for Lustre into an EKS pod via CSI | [`eks-csi-driver.md`](eks-csi-driver.md) |
| ML/inference sizing patterns and hydration order | [`ml-inference-patterns.md`](ml-inference-patterns.md) |
| Subnet placement, security groups, KMS, pod identity | [`networking-security.md`](networking-security.md) |
| Real cost/perf math for realistic model weights | [`cost-perf-benchmarks.md`](cost-perf-benchmarks.md) |
| A worked Terraform stack on EKS + FSx + Karpenter | [`terraform-eks-integration.md`](terraform-eks-integration.md) |
| Everything that has bitten teams in prod on this stack | [`gotchas-limits.md`](gotchas-limits.md) |

### 10.1 Canonical external references

- AWS FSx product page: <https://aws.amazon.com/fsx/>
- FSx for Lustre docs: <https://docs.aws.amazon.com/fsx/latest/LustreGuide/>
- FSx for OpenZFS docs: <https://docs.aws.amazon.com/fsx/latest/OpenZFSGuide/>
- FSx for NetApp ONTAP docs: <https://docs.aws.amazon.com/fsx/latest/ONTAPGuide/>
- FSx for Windows File Server docs: <https://docs.aws.amazon.com/fsx/latest/WindowsGuide/>
- FSx API reference: <https://docs.aws.amazon.com/fsx/latest/APIReference/Welcome.html>
- Amazon EKS User Guide, FSx CSI driver page: <https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi.html>
- `aws-fsx-csi-driver`: <https://github.com/kubernetes-sigs/aws-fsx-csi-driver>
- Lustre upstream: <http://lustre.org/> and manual at <https://doc.lustre.org/lustre_manual.xhtml>
- OpenZFS docs: <https://openzfs.github.io/openzfs-docs/>
- Mountpoint for Amazon S3 (Lustre alternative for read-heavy S3): <https://github.com/awslabs/mountpoint-s3>

---

## 11. A concrete example: same workload, four flavors

To ground the trade-offs, imagine we need to expose **10 TB of shared
data** to a **32-node Linux compute pool** at **5 GB/s aggregate** with
**submillisecond p50 read latency**, and we want it **recoverable across
AZs**.

### 11.1 Lustre approach

```hcl
resource "aws_fsx_lustre_file_system" "hot_cache" {
  storage_capacity              = 12000   # GB, minimum for PERSISTENT_2 SSD
  deployment_type               = "PERSISTENT_2"
  per_unit_storage_throughput   = 500     # MB/s/TiB -> 12 TiB * 500 = 6000 MB/s
  storage_type                  = "SSD"
  subnet_ids                    = [aws_subnet.compute_a.id]
  security_group_ids            = [aws_security_group.fsx_lustre.id]
  kms_key_id                    = aws_kms_key.fsx.arn
  data_compression_type         = "LZ4"

  tags = {
    Name = "hot-cache-lustre"
  }
}

# S3 backing for DR. See lustre-s3-drs.md for the DRA that hydrates from here.
resource "aws_s3_bucket" "hot_cache_source" {
  bucket = "hot-cache-source-${random_id.postfix.hex}"
}

resource "aws_s3_bucket_replication_configuration" "cross_region" {
  # ... standard CRR to a peer region, so the S3 side is DR-safe.
}
```

Pros: 6 GB/s baseline, ~50 GB/s burst from cache. Single-file parallel
reads. Native S3 DRA integration; nodes can spread across the AZ freely.

Cons: single-AZ; AZ-loss requires DRA re-hydration into a new FS in
another AZ. No SMB/Windows. Backups optional (via DRA + S3 versioning).

### 11.2 OpenZFS Multi-AZ approach

```hcl
resource "aws_fsx_openzfs_file_system" "shared" {
  storage_capacity     = 10240   # GiB
  storage_type         = "SSD"
  throughput_capacity  = 4096    # MBps
  deployment_type      = "MULTI_AZ_1"
  subnet_ids           = [aws_subnet.compute_a.id, aws_subnet.compute_b.id]
  preferred_subnet_id  = aws_subnet.compute_a.id
  security_group_ids   = [aws_security_group.fsx_nfs.id]
  kms_key_id           = aws_kms_key.fsx.arn

  root_volume_configuration {
    data_compression_type = "ZSTD"
    record_size_kib       = 128
  }
}
```

Pros: Multi-AZ HA. Instant snapshots and clones. Up to 21 GBps *cache*
throughput; 4 GBps sustained to disk. Standard NFS clients.

Cons: NFS single-client cap is meaningfully lower than Lustre's per-file
throughput. No S3 hydration story (S3 Access Points are the reverse
direction — expose the volume through S3, not import from S3).
Cross-AZ transfer costs on read from the standby AZ.

### 11.3 ONTAP approach

```hcl
resource "aws_fsx_ontap_file_system" "enterprise" {
  storage_capacity    = 10240
  throughput_capacity = 4096
  deployment_type     = "MULTI_AZ_1"
  subnet_ids          = [aws_subnet.compute_a.id, aws_subnet.compute_b.id]
  preferred_subnet_id = aws_subnet.compute_a.id
  security_group_ids  = [aws_security_group.fsx_ontap.id]
  kms_key_id          = aws_kms_key.fsx.arn
}

resource "aws_fsx_ontap_storage_virtual_machine" "svm" {
  file_system_id             = aws_fsx_ontap_file_system.enterprise.id
  name                       = "svm1"
  root_volume_security_style = "UNIX"
}

resource "aws_fsx_ontap_volume" "vol" {
  name                       = "shared"
  size_in_megabytes          = 10485760
  storage_virtual_machine_id = aws_fsx_ontap_storage_virtual_machine.svm.id
  junction_path              = "/shared"
  tiering_policy {
    name = "AUTO"
    cooling_period = 31
  }
}
```

Pros: Multi-AZ HA. NFS + SMB + iSCSI + NVMe/TCP on the same volume.
Capacity-pool tiering pushes cold blocks to S3-backed storage; you pay for
what's on SSD. SnapMirror to another region for DR.

Cons: highest management surface area and price. Overkill unless the
multi-protocol or enterprise-feature story lands you here.

### 11.4 Windows approach

```hcl
resource "aws_fsx_windows_file_system" "share" {
  storage_capacity                   = 10240
  storage_type                       = "SSD"
  throughput_capacity                = 2048
  deployment_type                    = "MULTI_AZ_1"
  subnet_ids                         = [aws_subnet.compute_a.id, aws_subnet.compute_b.id]
  preferred_subnet_id                = aws_subnet.compute_a.id
  security_group_ids                 = [aws_security_group.fsx_smb.id]
  active_directory_id                = aws_directory_service_directory.corp.id
  kms_key_id                         = aws_kms_key.fsx.arn
  automatic_backup_retention_days    = 30
  copy_tags_to_backups               = true
}
```

Pros: Multi-AZ HA. Native SMB, NTFS ACLs, AD auth. Windows-native tooling.

Cons: SMB from Linux is possible but not idiomatic; Windows semantics
don't map cleanly to a GPU Linux pod. Not the right substrate for a
compute-heavy ML workload.

### 11.5 The scoring

For the "shared file system for a Linux compute pool at 5 GB/s aggregate"
requirement:

| Flavor | Meets throughput? | Meets latency? | Multi-AZ? | Fit |
|---|---|---|---|---|
| Lustre | yes, cleanly | yes | no (needs S3 DRA CRR) | best on perf, weakest on native multi-AZ |
| OpenZFS | close (cache-hot yes, sustained borderline) | yes | yes | best on ops simplicity |
| ONTAP | yes | yes | yes | best on feature depth; highest price |
| Windows | no (SMB-only) | yes | yes | wrong tool |

If the workload is a **GPU inference fleet on EKS**, the S3-DRA story
usually tips the scale to Lustre and the Multi-AZ concern is handled at
the S3 layer. If the workload is **generic Linux batch with tight ops
budget**, OpenZFS Multi-AZ is often cleaner. If the workload is **regulated
data warehousing needing SnapLock or multi-protocol**, ONTAP.

---

## 12. Glossary

- **DRA** — Data Repository Association; a Lustre-specific object that ties
  a directory in the FS to an S3 prefix. See
  [`lustre-s3-drs.md`](lustre-s3-drs.md).
- **DNE** — Distributed Namespace Environment; Lustre's namespace-sharding
  feature. Exposed on FSx PERSISTENT_2 as provisionable Metadata IOPS.
- **EFA** — Elastic Fabric Adapter; RDMA-capable networking. On p5/p5e/etc.
  and used by FSx Lustre PERSISTENT_2 EFA deployment for high per-client
  bandwidth.
- **FlexVol / SVM** — NetApp ONTAP volume and storage-virtual-machine
  primitives.
- **LNet** — Lustre Networking; the RPC-over-{TCP,EFA} substrate.
- **MDT / OST** — Metadata Target and Object Storage Target; Lustre's
  persistent stores.
- **PFL** — Progressive File Layout; Lustre's default per-file striping
  policy that grows the stripe count as a file grows.
- **PV / PVC** — Kubernetes Persistent Volume and Claim, produced by the
  aws-fsx-csi-driver.
- **SnapMirror** — NetApp block-level asynchronous replication.
- **SnapLock** — NetApp WORM compliance.
- **VSS** — Volume Shadow Copy Service on Windows; used for FSx Windows
  and FSx ONTAP backup consistency.

---

_This note is a routing overview. It intentionally does not duplicate the
technical depth in the sibling notes; each of those is self-contained.
Start here if you don't yet know which FSx you want; then read the
Lustre-specific notes if that's where you land._
