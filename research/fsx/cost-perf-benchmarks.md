# FSx for Lustre — cost model, performance benchmarks, and sizing

> Research note for the `inference-clusters` monorepo. Focus: what FSx for Lustre actually
> costs, how fast it actually is, and how to size it against realistic inference and training
> workloads running on EKS. All prices are `us-east-1`, on-demand, from AWS's public
> pricing pages (see citations inline). Numbers are current as of the AWS docs versioned in
> late 2025 / early 2026 and should be re-verified against the
> [pricing page](https://aws.amazon.com/fsx/lustre/pricing/) before committing to a bill of
> materials.

## TL;DR

- **Throughput is bought by TiB, not by MB/s directly.** Aggregate baseline throughput is
  `provisioned_storage_TiB × PerUnitStorageThroughput_MBps_per_TiB`. On PERSISTENT‑2 SSD
  the top tier is 1000 MB/s per TiB; e.g. 4.8 TiB × 1000 = 4.8 GB/s baseline for a single
  file system. See [Performance characteristics of SSD and HDD storage
  classes](https://docs.aws.amazon.com/fsx/latest/LustreGuide/ssd-storage.html).
- **PERSISTENT‑2 is the modern SSD path** (125 / 250 / 500 / 1000 MB/s/TiB tiers).
  PERSISTENT‑1 (50 / 100 / 200 tiers) is previous‑generation and only creatable via CLI/API.
  SCRATCH‑2 is 200 MB/s/TiB baseline with 1300 MB/s/TiB burst — cheapest per‑TiB, but data
  isn't replicated. See [Deployment and storage class options](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html).
- **Per‑client throughput is capped by the client NIC and by Lustre OSS striping.** A single
  non‑EFA client tops out at ~100 Gbps to the file system, and any single client↔OSS pair
  is capped at 5 Gbps ([Throughput to individual client
  instances](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html#throughput-clients)).
  To saturate a fat FSx you need many clients and enough OSTs (stripe count) to spread the
  I/O across them.
- **LZ4 data compression is free and usually a net win for AI/ML.** It reduces stored bytes
  (and backup bytes) and, because on‑disk throughput is the tighter of the two limits at the
  lower tiers, it can multiply effective disk throughput up to the network throughput cap.
  Example from AWS: PERSISTENT‑50 disk baseline 50 MB/s/TiB can climb to the 250 MB/s/TiB
  network ceiling with LZ4 on. See
  [Lustre data compression](https://docs.aws.amazon.com/fsx/latest/LustreGuide/data-compression.html).
- **$/GB/month roughly ranks:** S3 Standard ≪ EFS IA ≈ S3 Express (storage‑only) ≪
  FSx PERSISTENT‑2 (125 MB/s tier) ≪ EFS Standard ≪ EBS gp3 (before IOPS/throughput add‑ons)
  ≪ FSx PERSISTENT‑2 (1000 MB/s tier). Instance‑local NVMe is "free" storage but ephemeral,
  local‑only, and priced into the instance‑hour.
- **Rules of thumb for inference clusters:**
  - Big weight fan‑out to many pods → S3‑direct or Mountpoint‑for‑S3 first; FSx only
    when the S3 GET fan‑out or first‑byte latency is a bottleneck.
  - Training datasets that are read >2× → FSx (or FSx‑linked S3 repository).
  - Shared checkpoints written from many ranks concurrently → FSx.
  - Everything write‑once, read‑a‑few‑times → S3.

## 1. Where FSx for Lustre fits

FSx for Lustre is a managed
[Lustre](http://lustre.org/) file system. Each file system is a collection of *metadata
targets* (MDTs) and *object storage targets* (OSTs) served by *object storage servers*
(OSSes). Clients mount it over the network via the standard Lustre client and use it as a
POSIX filesystem. The service handles the file server / disk replacement lifecycle and, for
persistent file systems, replicates the underlying disks. See
[What is Amazon FSx for
Lustre?](https://docs.aws.amazon.com/fsx/latest/LustreGuide/what-is.html) for the fully
managed description.

Three storage classes are available:

| Class | Media | Elasticity | Best for | Notes |
| --- | --- | --- | --- | --- |
| SSD | NVMe SSD | provisioned (you choose TiB) | Sub‑ms full‑dataset latency, many small IOPS | SCRATCH‑2, PERSISTENT‑1, PERSISTENT‑2 |
| Intelligent‑Tiering | tiered w/ optional SSD read cache | elastic (pay‑for‑what‑you‑store) | Mixed hot/cold data, cache‑friendly workloads | throughput provisioned in 4000 MB/s units |
| HDD | HDD + optional 20% SSD read cache | provisioned | Consistent single‑digit‑ms latency across many TiB | 12 or 40 MB/s/TiB tiers |

For a GPU inference cluster the SSD storage class (specifically PERSISTENT‑2) is almost
always what you want. Intelligent‑Tiering matters when data volumes get large and access
patterns are skewed. HDD is a distraction for GenAI workloads.

Two deployment types on SSD:

- **SCRATCH‑2** — no replication, cheaper per‑TiB, use for temp/derived data. 200 MB/s/TiB
  baseline, 1300 MB/s/TiB burst. Availability of a single scratch file system degrades with
  size, e.g. `4.8 TiB` = 99.8% / day, `50.4 TiB` = 99.1% / day
  ([Scratch file systems durability
  table](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html#scratch-file-system)).
- **PERSISTENT‑2** — replicated disks, auto‑replacement of failed file servers, 125 / 250 /
  500 / 1000 MB/s/TiB tiers. In `us-east-1` with EFA enabled you also get
  GPUDirect Storage support.

The rest of this document assumes SSD‑backed PERSISTENT‑2 unless called out.

## 2. Pricing model

### 2.1 Storage capacity: paid per GB‑month by tier

FSx for Lustre pricing is **the sum of a storage‑capacity charge and (for SSD tiers) a
throughput charge that is embedded in the per‑GB rate you pick**. In other words, you don't
buy "50 MB/s" as a separate line item on PERSISTENT‑2; you buy `X TiB of the 500 MB/s/TiB
tier`, and the throughput comes with the capacity. This is unlike EFS Elastic Throughput or
gp3 (see §7).

The rates AWS quotes at
[the pricing page](https://aws.amazon.com/fsx/lustre/pricing/) for `us-east-1` are:

| Tier / class | GB‑month | Included throughput | Effective $/MB/s/month* |
| --- | --- | --- | --- |
| SCRATCH‑2 SSD | $0.140 | 200 MB/s/TiB baseline (burst 1300) | — (implied) |
| PERSISTENT‑2 SSD, 125 MB/s/TiB | $0.145 (approx.) | 125 MB/s/TiB | — |
| PERSISTENT‑2 SSD, 250 MB/s/TiB | higher | 250 MB/s/TiB | — |
| PERSISTENT‑2 SSD, 500 MB/s/TiB | higher | 500 MB/s/TiB | — |
| PERSISTENT‑2 SSD, 1000 MB/s/TiB | highest | 1000 MB/s/TiB | — |
| PERSISTENT‑1 SSD (legacy), 50 / 100 / 200 MB/s/TiB | tier‑dependent | 50 / 100 / 200 | — |
| Intelligent‑Tiering — Frequent Access | $0.0230 | via provisioned throughput | separate |
| Intelligent‑Tiering — Infrequent Access | $0.0125 | " | " |
| Intelligent‑Tiering — Archive Instant | $0.0040 | " | " |
| Intelligent‑Tiering SSD read cache | $0.09 | — | — |
| Intelligent‑Tiering throughput capacity | — | provisioned separately | **$0.52 / MB/s / month** |
| Metadata IOPS above baseline | $0.055 / IOPS / month | — | — |
| Backup storage | $0.050 / GB / month | — | — |
| Monitoring & automation (IT) | $0.0006 / GB / month | — | — |
| IT read requests | $0.0004 / 1,000 | — | — |
| IT write requests | $0.0050 / 1,000 | — | — |

*Effective $/MB/s/month is a derived quantity because the SSD tiers bundle capacity and
throughput. It's meaningful only when you compare tiers at fixed capacity — see §2.4.

**Confirm current rates.** AWS occasionally rebalances FSx tier pricing. Check
[https://aws.amazon.com/fsx/lustre/pricing/](https://aws.amazon.com/fsx/lustre/pricing/)
before pinning numbers into an infrastructure planning document.

### 2.2 Minimum capacities and increments

Increments matter because "I need 500 MB/s" is really "I need at least 4 TiB at the
125 MB/s/TiB tier". Storage per OSS defines the minimum step:

| Deployment | MB/s/TiB tier | Storage per OSS | Minimum FS | Increment |
| --- | --- | --- | --- | --- |
| PERSISTENT‑2 EFA | 125 | 38.4 TiB per OSS | 1.2 TiB | 2.4 TiB |
| PERSISTENT‑2 EFA | 250 | 19.2 TiB per OSS | 1.2 TiB | 2.4 TiB |
| PERSISTENT‑2 EFA | 500 | 9.6 TiB per OSS | 1.2 TiB | 2.4 TiB |
| PERSISTENT‑2 EFA | 1000 | 4.8 TiB per OSS | 1.2 TiB | 2.4 TiB |
| PERSISTENT‑2 non‑EFA | 125/250/500/1000 | 2.4 TiB per OSS | 1.2 TiB | 2.4 TiB |
| PERSISTENT‑1 SSD | 50/100/200 | 2.4 TiB per OSS | 1.2 TiB | 2.4 TiB |
| SCRATCH‑2 | 200 | 2.4 TiB per OSS | 1.2 TiB | 2.4 TiB |
| Intelligent‑Tiering | 4000 MB/s per OSS | — | elastic | 4000 MB/s throughput steps |

Source: [IP addresses for file
systems](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html#ip-addesses-for-fs).

### 2.3 What "throughput charge" means on Intelligent‑Tiering

Intelligent‑Tiering decouples storage and throughput: you pay **$0.52 / MB/s / month** for
provisioned throughput (in 4000 MB/s increments — one OSS at a time) plus the per‑GB rate of
whichever access tier the data is in. That's a real "MB/s bought separately" line item, and
because throughput is auto‑scaled independently of capacity you can effectively buy 4 GB/s
against 1 PB of cold data. This is the class to compare against EFS Elastic Throughput.

### 2.4 Worked example: $/GB/month for a fixed workload

Take a 4.8 TiB file system (a natural increment) and compare tiers, holding capacity
constant:

```
Base capacity: 4.8 TiB × 1024 GiB/TiB = 4,915.2 GiB  ≈  4,915 GB
```

| Tier | $/GB‑month | Monthly storage $ | Aggregate baseline throughput | Effective $/GB/s of BW |
| --- | --- | --- | --- | --- |
| SCRATCH‑2 (200 MB/s/TiB) | 0.140 | $688 | 960 MB/s (1300 MB/s/TiB burst) | ~$717 /GB/s |
| PERSISTENT‑2 (125) | ~0.145 | $713 | 600 MB/s | ~$1,188 /GB/s |
| PERSISTENT‑2 (250) | ~0.235 | ~$1,155 | 1.2 GB/s | ~$963 /GB/s |
| PERSISTENT‑2 (500) | ~0.415 | ~$2,040 | 2.4 GB/s | ~$850 /GB/s |
| PERSISTENT‑2 (1000) | ~0.780 | ~$3,834 | 4.8 GB/s | ~$799 /GB/s |

The higher throughput tiers actually get *cheaper on a $/GB/s of aggregate throughput*
basis — you're paying for more disks per GiB. So if you actually need the throughput,
buying the top tier is more efficient than buying 10× as much storage at the lowest tier
just to reach the same aggregate MB/s. **Pick the tier by target throughput, then let the
minimum‑capacity increments dictate the actual TiB you provision.**

The exact PERSISTENT‑2 250/500/1000 rates above are illustrative; confirm at
[aws.amazon.com/fsx/lustre/pricing/](https://aws.amazon.com/fsx/lustre/pricing/).

### 2.5 Backups

Backups are per‑GB of *logical* file system size (with the compression discount applied
because compressed physical bytes are what's actually stored) at $0.050/GB‑month. Backups
are incremental after the first, so ongoing cost is proportional to churn, not to full FS
size.

## 3. Throughput math

### 3.1 The core formula

```text
aggregate_baseline_throughput_MBps = provisioned_TiB × perUnitStorageThroughput
```

Applied per deployment type ([AWS docs — SSD performance
tables](https://docs.aws.amazon.com/fsx/latest/LustreGuide/ssd-storage.html)):

| Deployment | Baseline MB/s per TiB (disk) | Baseline MB/s per TiB (network) | Burst network MB/s per TiB |
| --- | --- | --- | --- |
| SCRATCH‑2 | 200 read / 100 write | 200 | 1300 |
| PERSISTENT‑1 (50) | 50 | 250 | 1300 (530 in select regions) |
| PERSISTENT‑1 (100) | 100 | 500 | 1300 |
| PERSISTENT‑1 (200) | 200 | 750 | 1300 |
| PERSISTENT‑2 (125) | 125 | 320 | 1300 |
| PERSISTENT‑2 (250) | 250 | 640 | 1300 |
| PERSISTENT‑2 (500) | 500 | 1300 | — |
| PERSISTENT‑2 (1000) | 1000 | 2600 | — |

Note two ceilings: **disk throughput** and **network throughput**. When you read data that
already lives in the OSS memory or SSD cache the network limit governs; on a cache miss
you're bounded by the lower of the two. AWS's own diagram (linked from
[performance.html](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html)) is
worth internalising.

### 3.2 Burst behaviour and I/O credits

FSx for Lustre uses a **network I/O credit** mechanism (analogous to EBS gp3 burst credits):
when you use less than the baseline network throughput, credits accrue; when you use more,
you draw them down until the burst ceiling. SCRATCH‑2 and PERSISTENT‑1/2 up to 250 MB/s/TiB
advertise a 1300 MB/s/TiB network burst; PERSISTENT‑2 500 and 1000 MB/s/TiB tiers do **not**
publish a separate burst number — their baseline is already at or above the 1300 MB/s/TiB
ceiling. This is why a big provisioning of PERSISTENT‑2‑1000 provides sustained "burst‑
equivalent" behaviour without burst‑credit anxiety.

**Corollary for cold model loads.** If your workload is a burst — 32 pods pulling a 200 GB
model once every hour — the burst throughput is what matters, not the baseline. A 4.8 TiB
PERSISTENT‑2‑125 file system offers 4.8 × 320 = 1.5 GB/s network baseline and 4.8 × 1300 =
6.2 GB/s network burst. You can drain 200 GB in ~30 s if you can stripe the file wide
enough and clients don't dogpile a single OST (see §4).

### 3.3 Aggregate throughput per file system vs per client

Aggregate FS throughput scales linearly with `TiB × tier`. But an individual client instance
has hard caps ([Throughput to individual client
instances](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html#throughput-clients)):

| Client instance NIC | File system type | Max throughput per client |
| --- | --- | --- |
| Any | Non‑EFA FS | 100 Gbps (~12.5 GB/s) |
| ENA | EFA‑enabled FS | 100 Gbps |
| ENA Express | EFA‑enabled FS | 100 Gbps |
| EFA | EFA‑enabled FS | 700 Gbps (~87 GB/s) |
| EFA + GPUDirect Storage | EFA‑enabled FS | 1200 Gbps (~150 GB/s) |

And any single client-to-OSS stream is capped at **5 Gbps** — you have to spread I/O across
enough OSSes (i.e. enough striping) to reach the aggregate. See
[performance.html](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html#throughput-clients),
footnote about the 5 Gbps per client‑OSS pair, and
[IP addresses for file
systems](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html#ip-addesses-for-fs)
for how AWS counts OSSes.

## 4. IOPS and metadata

Data IOPS scale like throughput — measured in "tens of thousands baseline, hundreds of
thousands burst" per file system on SSD deployments. That number is only interesting for
random‑small workloads; deep learning I/O is almost always large sequential.

**Metadata IOPS** are the number you actually need to watch on inference clusters. Model
directories full of small shard files, tokenizer JSON, config files, and pod‑local
`__pycache__` directories all hammer metadata. On PERSISTENT‑2 you can now provision
metadata IOPS separately (Automatic or User‑provisioned mode). AWS's cost:

```
$0.055 per metadata IOPS-month above the baseline for the size tier
```

Baseline entitlement in Automatic mode ([File system metadata
performance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html#dne-metadata-performance)):

| FS storage | Included metadata IOPS |
| --- | --- |
| 1,200 GiB | 1,500 |
| 2,400 GiB | 3,000 |
| 4,800 – 9,600 GiB | 6,000 |
| 12,000 – 45,600 GiB | 12,000 |
| ≥ 48,000 GiB | 12,000 IOPS per 24,000 GiB |

Metadata op cost (operations per provisioned IOPS/s):

| Operation | Ops per provisioned IOPS |
| --- | --- |
| File create / open / close | 2 |
| File delete | 1 |
| Directory create / rename | 0.1 |
| Directory delete | 0.2 |

A 6,000 metadata‑IOPS baseline can therefore sustain ~12,000 opens/s or ~1,200 mkdir/s.
Directory operations are the expensive ones; if you have a build step generating tens of
thousands of nested dirs you'll want to bump metadata IOPS or restructure to flatter paths.

## 5. Per‑instance‑family saturation

The 100‑Gbps‑per‑non‑EFA‑client cap is the thing you hit first. Below is how many pods /
clients it takes to reach various FSx targets, given some common inference and training
instance families.

Network bandwidth per instance is authoritative from
[EC2 accelerated‑computing
specs](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html):

| Instance | Net (Gbps) | EFA | GDS‑EFA | Notional FSx read (GB/s) |
| --- | --- | --- | --- | --- |
| `c7i.24xlarge` | 37.5 | ✓ | — | ~4.7 |
| `c7i.48xlarge` | 50 | ✓ | — | ~6.25 |
| `g5.12xlarge` | 40 | ✓ | — | ~5.0 |
| `g5.24xlarge` | 50 | ✓ | — | ~6.25 |
| `g5.48xlarge` | 100 | ✓ | — | ~12.5 |
| `g6e.12xlarge` | 100 | ✓ | ✓ | ~12.5 (up to 87 via EFA path) |
| `g6e.48xlarge` | 400 | ✓ | ✓ | up to 50 (network) / 150 (EFA/GDS) |
| `p4d.24xlarge` | 4×100 | ✓ | — | ~50 |
| `p5.48xlarge` | 3200 (32×100) | ✓ | ✓ | up to ~150 GB/s per client with EFA+GDS |
| `p5e.48xlarge` | 3200 | ✓ | ✓ | up to ~150 GB/s per client with EFA+GDS |
| `p5en.48xlarge` | 3200 | ✓ | ✓ | up to ~150 GB/s per client with EFA+GDS |

The upshot: on non‑EFA read paths, **the file system saturates 1 GB/s per pod, per 10 GbE
of net**. On EFA + GDS the client itself is no longer the bottleneck, so the FS network
throughput ceiling matters again.

### 5.1 Approximate "clients needed to saturate FSx" chart

Assume clients are healthy `g5.48xlarge` non‑EFA nodes able to sustain ~10 GB/s each to the
FS. Aggregate baseline network throughput of the FS ÷ 10 GB/s ≈ clients required:

| FSx (TiB × tier) | Aggregate net baseline | Clients @ 10 GB/s |
| --- | --- | --- |
| 4.8 × PERSISTENT‑2‑125 | 1.5 GB/s | 1 (undersubscribed) |
| 4.8 × PERSISTENT‑2‑500 | 6.2 GB/s | 1 |
| 4.8 × PERSISTENT‑2‑1000 | 12.5 GB/s | 2 |
| 24 × PERSISTENT‑2‑500 | 31 GB/s | 4 |
| 24 × PERSISTENT‑2‑1000 | 62 GB/s | 7 |
| 96 × PERSISTENT‑2‑1000 | 250 GB/s | 25 |

For EKS pod count: multiply node count by (pods per node reading concurrently). A single
node cannot exceed its NIC, so packing more pods on it just splits the same bandwidth.

## 6. Data compression (LZ4)

FSx for Lustre supports LZ4 compression, which is Lustre‑community‑standard and cheap
CPU‑wise. Enabled via `DataCompressionType=LZ4` at file‑system create, or by
`aws fsx update-file-system --lustre-configuration DataCompressionType=LZ4` on an existing
FS (only newly written files are compressed; run
[`lfs_migrate`](https://github.com/aws-samples/fsx-solutions/blob/master/FSxL-Compression)
to compress historic data). Reference:
[Lustre data
compression](https://docs.aws.amazon.com/fsx/latest/LustreGuide/data-compression.html) and
the AWS Storage Blog post [Spend less while increasing performance with Amazon FSx for
Lustre data
compression](https://aws.amazon.com/blogs/storage/spend-less-while-increasing-performance-with-amazon-fsx-for-lustre-data-compression/).

### 6.1 Storage savings

Compression is applied on write and decompressed on read. `du` reports the physical
compressed size; `du --apparent-size` and `ls -l` report logical size. AWS's blog measured
these ratios:

| Data type | LZ4 ratio | Savings |
| --- | --- | --- |
| Random IOR benchmark data | 3.75:1 | 73% |
| `.pkl` pickled objects | 17.68:1 | 94% |
| `.h5` HDF5 tensors | 3.24:1 | 69% |
| `.json` structured configs | 7.75:1 | 87% |
| Pre‑compressed (`.gz`, `.zip`, `.bam`) | ~1:1 | ~0% |

Model weight files (`safetensors` fp16/bf16) typically compress **1.2–1.5×** — the mantissa
bits are already high‑entropy, but zero‑runs and repetitive metadata get squeezed. Tokenizer
JSONs and config files compress much better (5–10×) but they're small in absolute size.
Realistically, a Llama‑3‑70B safetensors tree compresses to ~70–85% of raw size.

### 6.2 Throughput effect

Compression reduces the number of bytes moved on the disk‑to‑OSS wire. Since **disk
throughput is the smaller of the two limits on lower tiers**, compression can lift effective
disk throughput up to the network throughput ceiling. AWS gives this example:

> If your file system is a PERSISTENT‑50 SSD deployment type, your network throughput has a
> baseline of 250 MBps per TiB of storage. Your disk throughput has a baseline of 50 MBps
> per TiB. With data compression, your disk throughput could increase from 50 MBps per TiB
> to a maximum of 250 MBps per TiB.

The Storage Blog benchmark ([Spend
less…](https://aws.amazon.com/blogs/storage/spend-less-while-increasing-performance-with-amazon-fsx-for-lustre-data-compression/))
reports IOR with random data:

| Config | Throughput |
| --- | --- |
| No compression | 3,560 MB/s |
| LZ4, baseline | 11,428 MB/s |
| LZ4, burst | 12,460 MB/s |

That's ~3.5× on synthetic data. Real workloads will land closer to `compression_ratio ×
disk_throughput`, capped by the network limit. For AI/ML the practical rule: **enable LZ4
unless your files are already gzip/webp/etc**.

### 6.3 CPU cost

LZ4 costs a couple percent of the OSS CPU budget; AWS advertises "no measurable impact on
latency". The client side does no compression work — decompression happens on the OSS.

## 7. Cost comparison vs S3, EFS, EBS, instance NVMe

All prices `us-east-1`, on‑demand.

### 7.1 $/GB‑month, storage only

| Service | Class | $/GB‑month |
| --- | --- | --- |
| **S3** | Standard, first 50 TB | 0.023 |
| S3 | Standard‑IA | 0.0125 |
| S3 | Intelligent‑Tiering Frequent | 0.023 |
| S3 | Intelligent‑Tiering Deep Archive | 0.00099 |
| **S3 Express One Zone** | Standard | 0.11 |
| **EFS** | Standard | 0.30 (Elastic Throughput) |
| EFS | Standard‑IA | 0.016 |
| EFS | Archive | 0.008 |
| EFS | One Zone | 0.16 |
| EFS | One Zone‑IA | 0.0133 |
| **EBS** | gp3 volume | 0.08 |
| EBS | gp3 provisioned IOPS above 3,000 | 0.005 / IOPS‑month |
| EBS | gp3 provisioned MB/s above 125 | 0.06 / MB/s‑month |
| EBS | io2 Block Express | 0.125 + IOPS/throughput |
| **FSx L** | SCRATCH‑2 SSD | 0.140 |
| FSx L | PERSISTENT‑2 SSD (125 MB/s/TiB) | ~0.145 |
| FSx L | PERSISTENT‑2 SSD (1000 MB/s/TiB) | ~0.78 |
| FSx L | Intelligent‑Tiering — Frequent | 0.023 |
| FSx L | Intelligent‑Tiering — Archive Instant | 0.004 |
| FSx L | IT throughput | 0.52 / MB/s‑month |
| Instance NVMe | e.g. p5.48xlarge 8×3.84 TB | included in $55.04/hr |

Sources: [S3 pricing](https://aws.amazon.com/s3/pricing/), [EFS
pricing](https://aws.amazon.com/efs/pricing/), [EBS
pricing](https://aws.amazon.com/ebs/pricing/), [FSx for Lustre
pricing](https://aws.amazon.com/fsx/lustre/pricing/), [EC2 On‑Demand
pricing](https://aws.amazon.com/ec2/pricing/on-demand/). The EFS Elastic Throughput adder is
priced separately at $6.00 per GB read and $30.00 per GB written when you exceed the
included allocation — check the EFS page for current numbers.

### 7.2 Total cost of ownership: worked example

**Scenario:** 5 TB dataset accessed by an 8‑node training job, one epoch reads 20 TB of
data (data augmentation, shuffling), job runs for 10 hours.

| Option | Monthly storage $ | Per‑epoch I/O $ | Notes |
| --- | --- | --- | --- |
| S3 Standard (streaming) | $115 (5 TB @ $0.023) | $0 (same region) + GET fee | GETs @ $0.0004 /1000; 20 TB in 4 MB chunks = 5M GETs = $2 |
| S3 Express One Zone | $550 (5 TB @ $0.11) | 20 TB × 8 workers all‑hot, low latency | request cost lower per‑op, ~80% cheaper than Standard requests |
| EFS Standard (Elastic) | $1,536 (5 TB @ $0.30) + read fees | reads billed at $0.03/GB → 20 TB × $0.03 × 1024 = ~$614 | expensive for hot data |
| FSx PERSISTENT‑2‑500 @ 9.6 TiB | ~$4,080 (9.6 TiB × $0.415) | $0 | 4.8 GB/s baseline network; 8 nodes × ~600 MB/s each easy |
| FSx PERSISTENT‑2‑500 + LZ4 | ~$3,300 physical | $0 | assume 1.5× ratio → 6.4 TiB physical bought |
| FSx SCRATCH‑2 @ 7.2 TiB | ~$1,032 (7.2 TiB × $0.140) | $0 | acceptable durability for one epoch |
| gp3 EBS 5 TB per node × 8 | ~$3,278 (40 TB × $0.08) | $0 | but data must be replicated to each node |

The EFS number is the killer — Elastic reads at $0.03/GB make it a bad fit for hot 20‑TB
epochs. S3 Standard is by far the cheapest storage‑bill option, and modern DL loaders (WebDataset,
[Mountpoint‑for‑S3](https://github.com/awslabs/mountpoint-s3), the
[SageMaker AI FSx recipe](https://aws.amazon.com/blogs/machine-learning/speed-up-training-on-amazon-sagemaker-using-amazon-efs-or-amazon-fsx-for-lustre-file-systems/))
routinely max out S3 network throughput per instance. FSx wins when (a) you're doing many
epochs (re‑reads amortise the higher $/GB), (b) you need low first‑byte latency across many
small files (embedding tables, tokenizer configs, small‑file image datasets), or (c) POSIX
semantics are non‑negotiable.

### 7.3 Access latency and IOPS class

| Service | First‑byte latency | Bandwidth per client | Random 4K IOPS class |
| --- | --- | --- | --- |
| S3 Standard | 100–200 ms | ≤ ~100 Gbps (with parallelism) | N/A (not a filesystem) |
| S3 Express One Zone | single‑digit ms | very high with parallelism | very high with parallelism |
| EFS Standard (Elastic) | single‑digit ms | scales elastically | ~500k+ read IOPS/FS |
| EBS gp3 | sub‑ms | 125–4000 MB/s / vol | 3000–16000 IOPS/vol |
| Instance NVMe | ~100 µs | GB/s per drive | hundreds of thousands IOPS |
| **FSx for Lustre SSD** | sub‑ms | up to 1200 Gbps w/ EFA+GDS | tens of thousands baseline, hundreds of thousands burst |

## 8. Sizing worked examples

### 8.1 Example A — 200 GB Llama‑3 weights served to 32 inference pods, target < 30 s cold load

**Ask.** 32 pods pull a 200 GB model on cold start; want < 30 s from PVC mount to weights
resident.

- Aggregate bytes to move: `32 × 200 GB = 6.4 TB` if each pod pulls its own copy (worst
  case), or `200 GB` if each pod reads the *same* bytes (pages served from the OSS cache
  after the first pod). Reality is somewhere in between: the first ~5 pods populate the OSS
  read cache and the rest read from RAM.
- Target time: 30 s → the shared portion (200 GB) has to arrive in `200 GB / 30 s ≈ 6.7
  GB/s`. Even without OSS caching, the same‑file semantics mean all 32 pods read the same
  blocks; the FS only has to move ~200 GB once from disk.
- Client cap: 32 × 100 Gbps = 3.2 Tbps aggregate = 400 GB/s aggregate ceiling. Not the
  bottleneck.

**Sizing.**

- Bandwidth requirement dominates capacity. 200 GB fits in the smallest 1.2 TiB FSx.
- 1.2 TiB × PERSISTENT‑2‑1000 = 1.2 GB/s disk baseline. Not enough; would take ~170 s.
- 1.2 TiB × PERSISTENT‑2‑1000 = 3.1 GB/s network baseline. Same ballpark — not enough.
- Two viable choices:
  1. **Use burst on a small SCRATCH‑2**: 1.2 TiB SCRATCH‑2 = 1.56 GB/s burst network per
     TiB × 1.2 = **1.9 GB/s burst**. Still not enough for 30 s if the data is truly cold.
  2. **Provision more TiB.** 4.8 TiB PERSISTENT‑2‑1000 = 4.8 GB/s baseline disk, 12.5 GB/s
     network baseline, room to absorb the 6.7 GB/s target with cache hits amortising the
     rest.
- With LZ4 compression on model files (typical ~1.3× ratio on safetensors): effective disk
  throughput lifts toward the network ceiling. Choose **4.8 TiB PERSISTENT‑2‑1000 with LZ4**.

**Cost.** ~$3,834/mo storage. Compare S3‑direct: 200 GB × 32 pods pulling once/hour = 6.4
TB/hour = 4.6 PB/mo, which stays in $0 (same region) but you're now measuring wall‑clock
against S3 GET throughput per pod (typically ~1 GB/s per warm pod with 20 concurrent
requests). Cold‑start 30 s is achievable via S3 with Mountpoint or `s5cmd` at ~1.5–2 GB/s
per pod — the bandwidth is there — but the *variance* is worse than FSx.

**Verdict.** For a 30 s SLO on 200 GB, either S3‑direct with heavy prefetch or a
`4.8 TiB PERSISTENT‑2‑1000` FSx will hit the target. FSx will hit it more consistently and
with lower per‑pod tuning. If you have 200+ different models rotating in and out, S3
scales better because you don't have to pin every model to a filesystem.

### 8.2 Example B — 5 TB training dataset, 8‑node training job

**Ask.** 8 GPU nodes (e.g. `p5.48xlarge`), 5 TB dataset, throughput bound by data loader
appetite (say 8 × 6 GB/s = 48 GB/s aggregate).

- **Capacity floor:** 5 TB = ~4.9 TiB. FSx minimum increment is 2.4 TiB, so 4.8 TiB (a
  full OSS on the 1000 tier) or 7.2 TiB.
- **Throughput floor:** 48 GB/s → need ≥ 48 GB/s baseline network on the FS. Using
  PERSISTENT‑2‑1000, that's 48/2.6 ≈ 18.5 TiB. Round to **19.2 TiB** (a natural full‑OSS
  boundary at the 1000 tier — 4 OSSes each with 4.8 TiB).
- **Client saturation:** at 6 GB/s per node, non‑EFA path (100 Gbps NIC = 12.5 GB/s) is
  fine. Even without EFA you have headroom.
- **Compression:** ML training data (images, tokenised text) compresses ~1.5–2×. With LZ4,
  disk throughput isn't the bottleneck — network is. Same 19.2 TiB provision holds.

**Cost.**

```
19.2 TiB × 1024 GB/TiB = 19,660 GB × ~$0.78/GB‑month = ~$15,300 /mo
```

If the job runs for 1 week/month, prorate to ~$3,825. Compare to same 5 TB on S3 Standard
(~$115/mo) plus request costs (~$5/epoch for 5M GETs). S3 wins on absolute cost by 30×.
**FSx pays for itself only if you have (a) many epochs, (b) small‑file workloads that trip
S3 latency, or (c) a train‑to‑result SLO that hinges on ~48 GB/s of aggregate throughput.**

### 8.3 Example C — Shared 10 TB checkpoints

**Ask.** Concurrent checkpoint writes from 8–16 rank‑0 processes, ~200 GB per checkpoint,
1 checkpoint/hour. Retain last 24 hours.

- **Capacity:** 10 TB retained → 10 TiB physical. Round to 12 TiB.
- **Throughput on write:** 8 ranks × 5 GB/s aim = 40 GB/s. Reality: checkpoints are usually
  IO‑bound at 1–3 GB/s per rank due to serialization; call it 20 GB/s aggregate.
- **Tier:** 12 TiB × 250 MB/s/TiB = 3 GB/s baseline network → insufficient. 12 TiB × 500 =
  6 GB/s → insufficient. 12 TiB × 1000 = 12.5 GB/s network baseline → still not enough for
  40 GB/s aim.
- Bump to **24 TiB × PERSISTENT‑2‑1000 = 25 GB/s network baseline**. This handles a
  20 GB/s write burst and leaves headroom for reads (resume from checkpoint).
- **Backups:** With daily backups and 200 GB churn/hour, incremental backup ~= 4.8 TB/day of
  changed data (write‑amplification factor of ~1). 4.8 TB × $0.05/GB = $240/day of backup
  storage growth, so plan retention accordingly.
- **Compression:** Model shard files compress well when they contain optimizer state
  (Adam moments have lower entropy than weights). Expect 1.3× on the checkpoint tree.

```hcl
resource "aws_fsx_lustre_file_system" "checkpoints" {
  storage_capacity            = 24576  # GiB
  subnet_ids                  = [aws_subnet.private_a.id]
  deployment_type             = "PERSISTENT_2"
  per_unit_storage_throughput = 1000    # MB/s/TiB
  data_compression_type       = "LZ4"

  # Match to a matching backup window.
  automatic_backup_retention_days   = 7
  daily_automatic_backup_start_time = "07:00"

  tags = merge(local.combined_tags, {
    Purpose = "checkpoints"
  })
}
```

## 9. K8s wiring: the CSI driver contract

FSx for Lustre exposes a
[CSI driver on EKS](https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi.html). Two
provisioning modes:

- **Static PV.** You precreate the FSx (Terraform), create a `PersistentVolume` referencing
  it, then `PersistentVolumeClaim`s bind. This is the sane mode for shared filesystems.
- **Dynamic PV.** A `StorageClass` provisions a *new* FSx per PVC. Rarely what you want at
  inference scale — an FSx per pod is absurd.

Example static PV+PVC referencing a pre‑existing FSx:

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: fsx-weights
  labels:
    app: llama3-inference
spec:
  capacity:
    storage: 4800Gi
  volumeMode: Filesystem
  accessModes: ["ReadWriteMany"]
  persistentVolumeReclaimPolicy: Retain
  csi:
    driver: fsx.csi.aws.com
    volumeHandle: fs-0123456789abcdef0
    volumeAttributes:
      dnsname: fs-0123456789abcdef0.fsx.us-east-1.amazonaws.com
      mountname: abcde
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: fsx-weights
  namespace: inference
spec:
  storageClassName: ""              # unbound → binds to matching PV above
  accessModes: ["ReadWriteMany"]
  resources:
    requests:
      storage: 4800Gi
  volumeName: fsx-weights
```

Mount tunables that matter for large sequential reads:

```yaml
volumeAttributes:
  dnsname: ...
  mountname: ...
  # See mount.lustre(8): flock lets applications advisory-lock across pods.
  # No mount options at the PV level in the CSI driver; set them via mountOptions
  # on the StorageClass or bake into the container entrypoint if you go static.
```

At high fan‑out, the `lfs setstripe -c` value governs how many OSTs a file will be striped
across. On file systems created after August 25 2023, PFL default is `1/8/16/32` across
size bands ([Striping data in your file
system](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html#striping-data)).
Under‑striping large files is the single most common perf pitfall. For 200 GB `safetensors`
model shards you want `-c -1` (stripe across all OSTs) or the default 32‑wide component in
the PFL.

## 10. Recommendations

For this repo's `eks-karpenter` inference clusters, the day‑1 posture already has S3 as the
canonical model store (see
[`platform_storage.tf`](../../libs/inference-tf-aws-eks-karpenter/inference_tf_aws_eks_karpenter/template/engine/platform_storage.tf)).
FSx would be an *opt‑in* additional storage plane, provisioned only when a workload's SLO
demands filesystem semantics or sustained > 10 GB/s to many pods.

Suggested defaults for a first FSx PV in this template:

```hcl
locals {
  fsx_capacity_gib = 4800   # 4.8 TiB — natural OSS boundary for 1000 MB/s/TiB
  fsx_tier_mbps    = 500    # PERSISTENT-2 500 MB/s/TiB — sane middle
}

resource "aws_fsx_lustre_file_system" "shared" {
  count = var.enable_fsx ? 1 : 0

  storage_capacity            = local.fsx_capacity_gib
  subnet_ids                  = [aws_subnet.private_a.id]  # single-AZ; users on private_b
                                                             # will pay cross-AZ ($0.01/GB)
  deployment_type             = "PERSISTENT_2"
  per_unit_storage_throughput = local.fsx_tier_mbps
  data_compression_type       = "LZ4"

  # See §4 for the metadata math.
  metadata_configuration {
    mode  = "USER_PROVISIONED"
    iops  = 6000
  }

  automatic_backup_retention_days = 7
  copy_tags_to_backups            = true

  tags = merge(local.combined_tags, {
    Purpose = "shared-inference-scratch"
  })
}
```

Guardrails:

- Keep FSx in the same AZ as the majority of pods; cross‑AZ hits $0.01/GB per direction.
- Enable LZ4 unconditionally. It's free and only helps.
- Provision metadata IOPS explicitly for workloads that create/delete lots of small files.
- Never provision an FSx per pod. Share it across the namespace or across the entire
  cluster.
- Measure OSS/OST balance via `lfs df -h` after burn‑in; re‑stripe hot files with
  `lfs migrate` if one OST is filling faster than the rest.
- Emit `LogicalDiskUsage` / `PhysicalDiskUsage` CloudWatch metrics and set alarms on
  compression ratio drops (indicates workloads writing pre‑compressed data).

## 11. Open questions and things to verify before committing

- **Latest PERSISTENT‑2 pricing.** The $/GB rates for the 250 / 500 / 1000 tiers in §2 are
  approximate. Confirm against the live pricing page.
- **Intelligent‑Tiering steady state.** For workloads with an obvious hot fraction, IT +
  SSD read cache can be cheaper than PERSISTENT‑2. Model with `access-frequency × object`
  distribution.
- **GPUDirect Storage.** On p5/p6/g6e nodes, EFA + GDS can push per‑client throughput past
  the standard 100 Gbps ENA cap. Requires the EFA‑enabled FSx variant and OSS storage per
  OSS is different (§2.2). Benchmark before quoting numbers.
- **Cross‑AZ.** Persistent‑2 SSD is single‑AZ. If your workload spans AZs, either replicate
  or budget for cross‑AZ traffic; consider FSx Intelligent‑Tiering (multi‑AZ replicated) or
  fall back to S3.
- **Scratch data loss tolerance.** SCRATCH‑2 is cheaper but not replicated. Do the arithmetic
  on
  [durability by size](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html#scratch-file-system)
  and match it against your job's rerun cost.

## References

- [Amazon FSx for Lustre pricing](https://aws.amazon.com/fsx/lustre/pricing/)
- [Amazon FSx for Lustre — Performance overview](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html)
- [Performance characteristics of SSD and HDD storage classes](https://docs.aws.amazon.com/fsx/latest/LustreGuide/ssd-storage.html)
- [Deployment and storage class options](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html)
- [Lustre data compression](https://docs.aws.amazon.com/fsx/latest/LustreGuide/data-compression.html)
- [Spend less while increasing performance with Amazon FSx for Lustre data compression (AWS Storage Blog)](https://aws.amazon.com/blogs/storage/spend-less-while-increasing-performance-with-amazon-fsx-for-lustre-data-compression/)
- [Managing metadata performance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/managing-metadata-performance.html)
- [FSx for Lustre CSI driver for EKS](https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi.html)
- [Mountpoint for Amazon S3](https://github.com/awslabs/mountpoint-s3)
- [FSxL‑Compression sample (aws‑samples)](https://github.com/aws-samples/fsx-solutions/blob/master/FSxL-Compression)
- [S3 pricing](https://aws.amazon.com/s3/pricing/)
- [EFS pricing](https://aws.amazon.com/efs/pricing/)
- [EBS pricing](https://aws.amazon.com/ebs/pricing/)
- [EC2 accelerated‑computing specs](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html)
- [Best practices design patterns: optimizing Amazon S3 performance](https://docs.aws.amazon.com/AmazonS3/latest/userguide/optimizing-performance.html)
- [Speed up training on Amazon SageMaker AI using Amazon FSx for Lustre and Amazon EFS](https://aws.amazon.com/blogs/machine-learning/speed-up-training-on-amazon-sagemaker-using-amazon-efs-or-amazon-fsx-for-lustre-file-systems/)
- Related in this repo: [`platform_storage.tf`](../../libs/inference-tf-aws-eks-karpenter/inference_tf_aws_eks_karpenter/template/engine/platform_storage.tf)
