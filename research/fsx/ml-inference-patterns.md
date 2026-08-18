---
title: "Optimization patterns — using FSx for Lustre with ML inference and training on EKS"
slug: ml-inference-patterns
category: fsx
audience: senior-infra
scope: EKS + FSx for Lustre for LLM / diffusion / MoE inference and training
last-reviewed: 2026-08-06
---

# Optimization patterns — using FSx for Lustre with ML inference and training on EKS

## TL;DR

- **Model weights belong on a shared, striped Lustre filesystem** the moment any single replica is more than ~5–10 GB or you have more than a handful of GPUs cold-starting concurrently. Per-node caches (S3 Mountpoint, NVMe instance store, EBS) win only when replicas are small enough that the cache primes in the time it takes Karpenter to bring the node up.
- **Provision FSx for Lustre by throughput, not capacity.** For an inference fleet whose worst-case failure mode is a cross-AZ scale-out event pulling `N × weightsGB` in parallel, size `PerUnitStorageThroughput` so that `capacity_TiB × throughput_MBps_per_TiB` ≥ `N × weightsGB / target_load_seconds`. A 1.2 TiB / PERSISTENT-1000 filesystem gives you 1.2 GB/s baseline; a 4.8 TiB / PERSISTENT-1000 gives 4.8 GB/s. Storage cost is dominated by the throughput tier, not the bytes stored.
- **Stripe large weight files across all OSTs** with `lfs setstripe -c -1 -S 4M` on the model directory *before* you copy weights in. The FSx default 4-component PFL is OK for mixed workloads but under-stripes 30–200 GB shards; stripe count must be set at create time.
- **Order matters: hydrate before scale-out.** A one-shot Kubernetes Job (`model-puller`) hydrates the FSx filesystem from S3 through a Data Repository Association (DRA) or `aws s3 cp`. Inference deployments block on a readiness Job/DRA task success gate; Karpenter only starts scaling GPU nodes when the FS is warm. This turns p99 model-load into a per-node network read, not an S3 GET storm.
- **Client tuning is not optional at 100+ Gbps.** Set `osc.*.max_rpcs_in_flight=32`, `mdc.*.max_rpcs_in_flight=64`, disable client-side checksums for read-heavy inference, and mount with `flock` only where you actually need it. See [FSx performance tips](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance-tips.html).
- **Use safetensors + `mmap`** on the pod side. With FSx as backing store, `safetensors` `safe_open(..., framework="pt", device=0)` translates to `mmap()` plus `pread()` on a striped file, which is the fastest cold-load path short of GDS.

---

## 1. Problem statement

Modern inference services on EKS have one dominant startup cost: **paging model weights off durable storage into GPU HBM**. For a Llama-3-70B model at fp16 that is roughly 140 GB per replica; a Mixtral 8x22B at fp16 is ~280 GB per replica; SDXL is 6–7 GB plus VAE, refiner, and (increasingly) fine-tuned LoRAs. Multiply by every replica cold-starting during a scale-out or a node replacement and the aggregate I/O demand is easily hundreds of GB/s across a fleet.

The interesting question is not "can we mount S3?" — it is: **given a cold GPU node dropping into a Deployment at t=0, when can it serve its first token, and at what $/GB/month for the storage backing?** Those two numbers form the entire design space this note covers.

### 1.1 Constraints we accept

- **EKS + Karpenter**, self-managed nodes, provisioned on demand (this repo's `inference-tf-aws-eks-karpenter` template).
- **GPU instances** — `p5.*`, `p4d.24xlarge`, `g6e.*`, `trn1.*`, `inf2.*`. Instance store where present, EFA/EFA-express where present.
- Weights change **rarely** (weekly to quarterly). Datasets and checkpoints change **frequently** during training. Fine-tuned adapters (LoRA/QLoRA) may change hourly.
- Inference SLO: p99 pod-ready time is what customers see. Training SLO: sustained aggregate read throughput and per-checkpoint save wall-clock.

### 1.2 What "fast" looks like for the top models

| Model | Weights (fp16, no quant) | Weights (int4 GGUF) | Common inference server |
|---|---|---|---|
| SDXL 1.0 base + refiner + VAE | ~13 GB | n/a in production | ComfyUI, diffusers |
| Llama-3-8B | 16 GB | 4.7 GB | vLLM, TGI, llama.cpp |
| Llama-3-70B | 140 GB | 35 GB | vLLM, TGI |
| Llama-3.1-405B | 810 GB (bf16) | 200 GB | vLLM (tensor parallel across nodes) |
| Mixtral 8x7B | 90 GB | 26 GB | vLLM |
| Mixtral 8x22B | 280 GB | 76 GB | vLLM |
| DeepSeek-V3 | 1.3 TB (fp8) | 350 GB | vLLM, SGLang |

For a Mixtral 8x22B replica loading over a single 100 Gbps NIC, the theoretical wall-clock is **~22 s** to move 280 GB at line rate. In practice, without striping and client tuning, you will see 90–180 s. That gap is what this note is about.

---

## 2. Storage taxonomy: what's actually available in an EKS cluster

Any serious optimization starts with knowing where the bits live. On an EKS node you have five plausible substrates, in decreasing latency-per-GB:

1. **NVMe instance store** — on `p5.*`, `p4d.24xlarge`, `g6e.*`, `trn1.*`, ephemeral, up to ~30 GB/s aggregate, ~100 µs latency. Free (bundled). Gone at node terminate.
2. **EBS gp3 / io2** — durable at pod granularity via `ebs.csi.aws.com`, 1000 MB/s per volume default, up to 4 GB/s io2 Block Express. AZ-pinned.
3. **FSx for Lustre** — shared POSIX, up to `1200 Gbps` per client with EFA + GDS ([performance docs](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html)), scales with capacity × per-unit throughput.
4. **S3 Mountpoint** (`s3.csi.aws.com`) — file-like access to S3 via [mountpoint-s3](https://github.com/awslabs/mountpoint-s3), sequential-read biased, weak POSIX. Free per byte transferred within region; you pay per-request.
5. **EFS** — regional multi-AZ NFS, elastic throughput up to ~20 GB/s, single-file bandwidth capped, ~600 µs–ms latency.

Plus the bucket types the mounts wrap:

- **S3 Standard** — the durable authority. 11 nines. Regional. ~50–150 ms first-byte, high tail.
- **S3 Express One Zone (directory buckets)** — single-digit ms latency, single-AZ, [80% cheaper requests than S3 Standard](https://aws.amazon.com/s3/storage-classes/express-one-zone/), storage price much higher per GB. Naming: `<base>--<az-id>--x-s3`. See [directory bucket overview](https://docs.aws.amazon.com/AmazonS3/latest/userguide/directory-buckets-overview.html).

### 2.1 Rough cost per GB per month (`us-east-1`, list prices, indicative)

| Substrate | List $/GB-month | Notes |
|---|---|---|
| S3 Standard | ~0.023 | + $0.0004 / 1k GETs |
| S3 Express One Zone | ~0.16 | 1 AZ; request cost ~1/8 of Standard |
| EBS gp3 | 0.08 | + $0.005 per provisioned MB/s over 3000 |
| EFS Standard | 0.30 | + $0.03 per provisioned MB/s (Elastic Throughput) |
| FSx Lustre SSD PERSISTENT-125 | ~0.145 | includes throughput |
| FSx Lustre SSD PERSISTENT-250 | ~0.20 | |
| FSx Lustre SSD PERSISTENT-500 | ~0.29 | |
| FSx Lustre SSD PERSISTENT-1000 | ~0.53 | |
| FSx Lustre Intelligent-Tiering (hot) | ~0.145 | + SSD read-cache $ + request tier $ |
| NVMe instance store | included in EC2 | ephemeral |

Prices drift; use the pricing page. The point is that **FSx Lustre PERSISTENT-1000 is ~23× the $/GB of S3 Standard, but every dollar buys throughput.** You buy Lustre for MB/s, not for TB.

### 2.2 Latency and throughput at-a-glance

| Substrate | First-byte latency (p50) | Peak per-client throughput | Aggregate ceiling | Cost model |
|---|---|---|---|---|
| NVMe instance store | ~100 µs | 30 GB/s (P5) | per node | free |
| EBS gp3/io2 | ~1 ms | 1–4 GB/s | per volume | GB + prov MB/s |
| FSx Lustre (EFA + GDS) | sub-ms | 150 GB/s | multi-TB/s | GB × PUST |
| FSx Lustre (ENA) | 1–5 ms | 12.5 GB/s | multi-TB/s | GB × PUST |
| Mountpoint S3 Standard | 40–150 ms first byte, then GB/s streaming | ~100 Gbps per instance | bucket TPS | GB + req |
| Mountpoint S3 Express | 2–10 ms first byte | ~100 Gbps per instance | 2M req/s per bucket | GB (higher) + req (lower) |
| EFS | ~600 µs–5 ms | ~5 GB/s per client | ~20 GB/s | GB + prov MB/s |

The [FSx per-client throughput table](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html) is worth memorizing:

- Non-EFA: 100 Gbps (single OSS pair caps at 5 Gbps, so you *need* striping to reach this).
- EFA + ENA: 100 Gbps.
- EFA-native: 700 Gbps.
- EFA + NVIDIA GPUDirect Storage: **1200 Gbps**.

At GDS speeds you are quite literally moving bytes from Lustre OSTs into GPU HBM without transiting host DRAM. This is what makes multi-TB/s fleet-wide throughput physically possible.

---

## 3. When to use FSx for Lustre vs the alternatives

There is no universal answer; the axis is (a) **how many replicas share the same bytes**, (b) **how long the bytes live**, and (c) **whether you need POSIX semantics** (mmap, `pwrite`, rename, DCP).

### 3.1 Decision matrix

| Access pattern | Best primary storage | Fallback |
|---|---|---|
| Cold-start SDXL (<20 GB weights) once per node, weights change monthly | S3 Mountpoint (with `--cache /mnt/nvme` per node) or `initContainer` `aws s3 cp` to `emptyDir` on NVMe | FSx Lustre if replicas > ~30 concurrent |
| Cold-start Llama-70B / Mixtral 8x22B, many replicas, models change weekly | **FSx Lustre + DRA** with weights pre-hydrated | S3 Express + Mountpoint if replicas rarely land on cold nodes |
| Cold-start Llama-405B / DeepSeek-V3 across multi-node tensor parallel groups | **FSx Lustre**, PERSISTENT-1000, striped -1 | none realistic |
| Training checkpoint save every N steps, sharded DCP across ranks | **FSx Lustre**, striped -1, `flock` mount | EFS (slower, no shared-write scaling) |
| Read-only training dataset shared by hundreds of workers | **FSx Lustre with S3 DRA** (lazy-load) | Mountpoint with per-node cache |
| Per-replica ephemeral scratch, GPU caches, KV-cache spill | **NVMe instance store**, `emptyDir` medium=Memory for tmpfs | ephemeral EBS |
| Small (<200 MB) LoRAs / prompts / configs fetched at inference start | S3 Express + `boto3` from pod | tmpfs |

### 3.2 The "one-shot vs recurring" split

Model weights are the classic **write-once, read-many-times-per-node** payload. You want the write cost concentrated in a batch import and every subsequent read to be pure network. That is exactly the DRA / one-shot import job pattern in §7.

Training checkpoints and datasets are **write-many, read-many**. That is where FSx's shared-writable POSIX filesystem earns its $/GB — you can pay it once and let hundreds of workers coordinate with `flock`, atomic renames, and PyTorch DCP concurrent writes.

---

## 4. FSx for Lustre sizing and provisioning knobs

### 4.1 Capacity, throughput, and metadata IOPS

FSx SSD file systems (PERSISTENT_2 is the current generation) let you buy three independent things:

1. **Storage capacity**, in TiB. Increments of 1.2 TiB for legacy SCRATCH_2/PERSISTENT_1; 2.4 TiB minimum + multiples of 2.4 TiB for PERSISTENT_2.
2. **Per-unit storage throughput (PUST)**, in MB/s per TiB. Valid values on PERSISTENT_2 SSD: **125, 250, 500, 1000** MB/s/TiB. See the [SSD storage class page](https://docs.aws.amazon.com/fsx/latest/LustreGuide/ssd-storage.html).
3. **Metadata IOPS**, in Automatic or User-Provisioned mode. Valid values 1500, 3000, 6000, 12000, or multiples of 12000 up to 192000.

Aggregate file-system throughput = capacity_TiB × PUST. So:

- 4.8 TiB × 250 MB/s/TiB = 1200 MB/s baseline
- 9.6 TiB × 500 MB/s/TiB = 4800 MB/s baseline
- 12.0 TiB × 1000 MB/s/TiB = 12 GB/s baseline

**Rule of thumb for inference fleets**: size for the worst-case simultaneous cold-scale event. If you expect at most `K` replicas to cold-start within `T` seconds and each pulls `W` GB, target `K × W / T` MB/s of *baseline* throughput. Then multiply by 1.3× to keep the working set inside the read cache RAM (Lustre gives you 3.4–27.3 GiB of RAM per TiB depending on PUST — see [SSD performance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/ssd-storage.html)).

Metadata IOPS matter for training and dataset workloads (millions of small files); for weight loading the file count is ~O(50) and default Automatic IOPS is fine.

### 4.2 Terraform sketch

The template in `libs/inference-tf-aws-eks-karpenter` does not currently declare an FSx filesystem; below is how it would look, consistent with this repo's conventions (`variables.tf` variable-only, defaults in `presets/defaults-all.tfvars`, `random_id.postfix` name embedding).

```hcl
# engine/modules/fsx-lustre/main.tf
resource "aws_fsx_lustre_file_system" "weights" {
  storage_type                    = "SSD"
  deployment_type                 = "PERSISTENT_2"
  per_unit_storage_throughput     = var.fsx_per_unit_throughput   # 125|250|500|1000
  storage_capacity                = var.fsx_storage_capacity_tib * 1024
  subnet_ids                      = [var.fsx_subnet_id]
  security_group_ids              = [aws_security_group.fsx.id]
  data_compression_type           = "LZ4"
  file_system_type_version        = "2.15"
  automatic_backup_retention_days = 0
  metadata_configuration {
    mode  = var.fsx_metadata_mode                                 # AUTOMATIC | USER_PROVISIONED
    iops  = var.fsx_metadata_iops                                 # only when USER_PROVISIONED
  }
  tags = merge(var.common_tags, { Name = "${local.template_name}-weights-${random_id.postfix.hex}" })
}

resource "aws_fsx_data_repository_association" "weights_dra" {
  file_system_id       = aws_fsx_lustre_file_system.weights.id
  file_system_path     = "/weights"
  data_repository_path = "s3://${var.weights_bucket}/models/"
  batch_import_meta_data_on_create = true
  s3 {
    auto_import_policy { events = ["NEW", "CHANGED"] }   # keep weights synced from S3
    auto_export_policy { events = [] }                    # weights fs is read-mostly
  }
}
```

Note: [only 8 DRAs per file system](https://docs.aws.amazon.com/fsx/latest/LustreGuide/create-dra-linked-data-repo.html), and only one DRA request can be in flight at a time. Structure your model hub as a single S3 prefix per FSx directory rather than a DRA per model.

### 4.3 Placement — one AZ, close to compute

Lustre file systems live in a single subnet. Deploy FSx in the **same AZ** as your GPU NodePool; cross-AZ Lustre traffic is possible but you pay $/GB per direction and lose EFA line-rate. If you run multi-AZ inference fleets, provision one FSx per AZ and have the weights DRA point every filesystem at the same S3 prefix.

Security group: allow TCP **988** (LNET) and **1018–1023** (Lustre control) inbound from the EKS node SG to the FSx ENIs. Include the same on outbound from the nodes.

### 4.4 EFA and GPUDirect Storage

If you provisioned the FSx with `throughput_capacity > 10 GBps` (i.e., `capacity_TiB × PUST_MBps_per_TiB ≥ 12000`), FSx will support EFA on the client side automatically. With GDS-capable NVIDIA GPUs (H100, H200, B100/B200 — anything with the `cufile` library and CUDA 12+) you can do direct-to-HBM reads. This bypasses host DRAM and, per AWS's [performance docs](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html), pushes per-client throughput to 1200 Gbps.

For EKS, this requires:

- EKS-optimized AMI with the Lustre client 2.15 (Amazon Linux 2023 or Ubuntu 22.04 with the 6.5/6.8 kernel).
- `cufile.json` in the pod plus the `libcufile.so` mount from the driver.
- FSx mount option `flock` disabled where possible for the weights filesystem (writes are one-shot).

---

## 5. Striping: the single biggest performance lever

Lustre spreads files across **Object Storage Targets (OSTs)**. Each 1.2 TiB of PERSISTENT_2 SSD capacity is roughly one OST. A file with `stripe_count = 1` reads at the throughput of a *single* OST no matter how big the filesystem is; a file with `stripe_count = -1` reads across every OST in parallel.

FSx file systems created after August 25 2023 have this default PFL:

```
lfs setstripe -E 100M -c 1 -E 10G -c 8 -E 100G -c 16 -E -1 -c 32 /fsx
```

That is:
- Files ≤ 100 MB: 1 stripe.
- 100 MB–10 GB: 8 stripes.
- 10 GB–100 GB: 16 stripes.
- 100 GB+: 32 stripes.

**For model weights this is fine but not optimal.** A typical Llama-70B safetensors sharded checkpoint has 15–30 files, each ~5 GB (falls into the 8-stripe bucket). If your FSx has 8 OSTs and you get 8-way striping, throughput is limited to 8 × per-OST-MBps. Bump the model directory explicitly:

```bash
# BEFORE copying weights in — layout is set at file create time
mkdir /fsx/weights/llama3-70b
lfs setstripe -c -1 -S 4M /fsx/weights/llama3-70b
```

- `-c -1` = stripe across all OSTs.
- `-S 4M` = 4 MiB stripe size. The default 1 MiB is fine but 4 MiB reduces per-stripe roundtrips for the big sequential reads that dominate weight loading.

Verify with:

```bash
lfs getstripe /fsx/weights/llama3-70b/model-00001-of-00030.safetensors
```

If the file already exists with the wrong layout, `lfs migrate -c -1 -S 4M <file>` rewrites it in place.

### 5.1 Striping cheatsheet

| Workload | `-c` (stripe count) | `-S` (stripe size) | Rationale |
|---|---|---|---|
| Weight shards (100 MB – 100 GB) | `-1` | `4M` | Maximize parallel OST reads. |
| Training dataset, mixed sizes | default PFL | default | Small files avoid the RPC-per-OST tax. |
| DCP checkpoint shards | `-1` | `4M` | Each rank writes its own file; the fs still fans out. |
| Very small config files (<10 MB) | `1` | `1M` | Multi-OST layout costs more than it saves. |
| Append-mode log files | `1` | default | FSx forces `-c 1` on `O_APPEND` files; don't fight it. |

### 5.2 Files imported from S3

When a DRA imports a file lazily, FSx uses the file-system's `ImportedFileChunkSize` (default 1 GiB) to stripe. A 30 GB weight shard imports as `(30 GiB / 1 GiB) + 1 = 31` stripes — reasonable, but if your FSx has fewer OSTs it caps at the OST count. If you want to guarantee `-c -1` for imported files:

1. `lfs setstripe -c -1` on the directory *before* the import runs (the import will honor the parent directory's default layout).
2. Or run a post-import `lfs migrate` job.

### 5.3 Progressive File Layout for mixed workloads

For a shared FS that holds both weights and datasets:

```bash
# Stripe count based on file size, larger stripe size for big reads
lfs setstripe \
  -E 1M   -c 1  -S 1M \
  -E 1G   -c 8  -S 1M \
  -E 50G  -c 16 -S 4M \
  -E -1   -c -1 -S 4M \
  /fsx
```

---

## 6. Client-side Lustre tuning

FSx assumes a modern (2.15) Lustre client. On EKS, you install the client into the AMI or as a DaemonSet-driven `lctl` tuner. This repo's Karpenter template can bake the tuning into a `bootstrap.sh` user-data addition.

### 6.1 Kernel + module knobs (persist across reboots)

Ship a MachineConfig or user-data snippet on the EKS AMI:

```bash
# /etc/modprobe.d/lustre.conf  — persists across reboot
options ptlrpc ptlrpcd_per_cpt_max=32
options ksocklnd credits=2560
```

Then at boot:

```bash
# /etc/systemd/system/lustre-tune.service
[Unit]
Description=FSx Lustre client tuning
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/lustre-tune.sh
[Install]
WantedBy=multi-user.target
```

Where `lustre-tune.sh` is:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Per FSx guidance for >64 vCPU clients
lctl set_param osc.*OST*.max_rpcs_in_flight=32
lctl set_param mdc.*.max_rpcs_in_flight=64
lctl set_param mdc.*.max_mod_rpcs_in_flight=50

# Large-memory nodes: LRU
CPU=$(nproc)
lctl set_param ldlm.namespaces.*.lru_max_age=600000
lctl set_param ldlm.namespaces.*.lru_size=$((100 * CPU))

# statahead for directory listing
lctl set_param llite.*.statahead_max=512
lctl set_param llite.*.statahead_agl=1
lctl set_param llite.*.statahead_xattr=1 || true

# Write behavior — 512 MiB dirty cap per OSC is generous
lctl set_param osc.*.max_dirty_mb=512

# Disable client-side end-to-end checksums for read-mostly weights fs.
# Lustre still integrity-checks on wire via TCP/EFA. Saves ~5-10% CPU on cold reads.
lctl set_param osc.*.checksums=0
```

Reference: [Amazon FSx for Lustre Performance Tips](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance-tips.html).

### 6.2 Mount options

```
<fs-id>.fsx.<region>.amazonaws.com@tcp:/<mountname>  /fsx  lustre  defaults,noatime,flock,_netdev  0  0
```

- `noatime` — mandatory. `atime` writes on every read kill metadata performance.
- `flock` — enable if any consumer uses `fcntl(F_SETLK)` (torch DCP does not require this, but some HuggingFace loaders do lock files during download). If you're purely read-only serving, drop it.
- `_netdev` — systemd waits for network before mounting.
- Do **not** enable `localflock` — false-shares locks with other clients.

### 6.3 Verify

After a first big read:

```bash
lctl get_param osc.*.stats            # rpcs_in_flight histogram
lctl get_param llite.*.read_ahead_stats
lctl get_param llite.*.max_read_ahead_mb
```

Set `llite.*.max_read_ahead_mb` to `512` if you're doing very large sequential reads (safetensors mmap does).

---

## 7. Hydration patterns — getting weights onto Lustre before the fleet needs them

The naive path is "point the pod at S3, let it stream." That is the wrong answer at scale because every replica competes for S3 GET quota and cross-AZ bandwidth. Do it the other way: **hydrate FSx once from a controlled job, then serve every pod read from Lustre.**

### 7.1 Pattern A — DRA lazy-load (default)

Create a DRA (see §4.2 Terraform). Metadata is imported at DRA creation time; **file data streams from S3 on first read**. This works fine for training datasets (workers naturally spread across the corpus) but is *bad* for concurrent cold-scale of inference (every replica racing for the same file bottlenecks on the OST responsible for that file).

### 7.2 Pattern B — One-shot DRA import task (recommended for weights)

Force-hydrate every byte:

```bash
aws fsx create-data-repository-task \
  --file-system-id fs-0abc \
  --type IMPORT_METADATA_FROM_REPOSITORY \
  --paths "s3://models-bucket/llama3-70b/"
```

Then a separate task to preload data (Lustre HSM restore):

```bash
aws fsx create-data-repository-task \
  --file-system-id fs-0abc \
  --type RELEASE_DATA_FROM_FILESYSTEM \  # if replacing an old version
  --paths "/weights/llama3-70b-old"

aws fsx create-data-repository-task \
  --file-system-id fs-0abc \
  --type IMPORT_METADATA_FROM_REPOSITORY \
  --paths "s3://models-bucket/llama3-70b/"
```

Wrap this in a Kubernetes Job that runs to completion and blocks the inference Deployment via an `initContainer` polling `aws fsx describe-data-repository-tasks`:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: hydrate-llama3-70b
spec:
  backoffLimit: 2
  template:
    spec:
      serviceAccountName: fsx-hydrator   # Pod Identity with fsx:*Task*, s3:GetObject
      restartPolicy: OnFailure
      containers:
      - name: hydrator
        image: amazon/aws-cli:2
        command:
        - /bin/bash
        - -c
        - |
          set -euo pipefail
          TASK_ID=$(aws fsx create-data-repository-task \
            --file-system-id $FS_ID \
            --type IMPORT_METADATA_FROM_REPOSITORY \
            --paths "s3://$BUCKET/$PREFIX/" \
            --report Enabled=false \
            --query 'DataRepositoryTask.TaskId' --output text)
          echo "Task $TASK_ID started"
          while true; do
            STATE=$(aws fsx describe-data-repository-tasks \
              --task-ids $TASK_ID \
              --query 'DataRepositoryTasks[0].Lifecycle' --output text)
            [[ "$STATE" == "SUCCEEDED" ]] && break
            [[ "$STATE" == "FAILED"    ]] && exit 1
            sleep 15
          done
```

### 7.3 Pattern C — Model-puller sidecar / initContainer

For clusters where DRA isn't set up, use a plain `initContainer` that pulls to `/fsx/weights/<model>/`:

```yaml
initContainers:
- name: model-puller
  image: amazon/aws-cli:2
  command:
  - /bin/bash
  - -c
  - |
    set -euo pipefail
    DST=/fsx/weights/llama3-70b
    if [[ -f $DST/.hydrated ]]; then
      echo "Already hydrated"; exit 0
    fi
    mkdir -p $DST
    lfs setstripe -c -1 -S 4M $DST || true    # in case parent didn't have it
    aws s3 sync --only-show-errors \
      --cli-read-timeout 0 \
      s3://models-bucket/llama3-70b/ $DST/
    sync
    touch $DST/.hydrated
  volumeMounts:
  - name: fsx-weights
    mountPath: /fsx/weights
```

**Idempotency guard**: the `.hydrated` sentinel prevents every pod from re-syncing on rescheduling. Because Lustre is a shared FS, the first pod to land wins; subsequent pods no-op. Use a `leases.coordination.k8s.io/Lease` or a small `Job` if you want stricter mutual exclusion.

### 7.4 Pattern D — Warm-pool controller

For predictable release cadences, run a controller that:

1. Watches a `ModelVersion` CR.
2. On new version, kicks off a hydration Job into `/fsx/weights/<model>/<version>`.
3. Once hydrated, patches the inference Deployment env `MODEL_PATH=/fsx/weights/<model>/<version>`.
4. Rolls the Deployment. Karpenter scales new nodes; each mounts already-warm FSx.
5. GCs old versions after N days.

This is exactly the pattern SageMaker HyperPod uses under the hood — see the [HyperPod model deployment announcement](https://aws.amazon.com/blogs/machine-learning/amazon-sagemaker-hyperpod-launches-model-deployments-to-accelerate-the-generative-ai-model-development-lifecycle/) which describes staging models on FSx to avoid download delays during scaling.

### 7.5 Pattern E — Two-tier: FSx + per-node NVMe cache

For very hot models where you want *sub-second* cold-start on already-provisioned nodes, add a per-node NVMe cache in front:

```yaml
initContainers:
- name: nvme-warm
  image: amazon/aws-cli:2
  command:
  - /bin/bash
  - -c
  - |
    if [[ -f /nvme/model/.hydrated ]]; then exit 0; fi
    mkdir -p /nvme/model
    cp -r /fsx/weights/llama3-70b/. /nvme/model/
    touch /nvme/model/.hydrated
  volumeMounts:
  - { name: fsx-weights, mountPath: /fsx/weights, readOnly: true }
  - { name: nvme,        mountPath: /nvme }
```

The vLLM container then points `--model /nvme/model` and re-loads at ~30 GB/s local. On P5 nodes with 30 TB of NVMe you can keep dozens of hot models resident.

---

## 8. Ordering with Karpenter node bring-up

The classic anti-pattern:

1. HPA scales replicas from 4 → 16.
2. Karpenter provisions 12 new nodes.
3. All 12 pods hit S3 simultaneously to `aws s3 cp` the model.
4. First-byte latency spikes; some GETs 503; pods take 2–5 minutes each to become Ready.
5. HPA re-evaluates, spikes further.

The right ordering:

1. **T0**: Operator applies a new `ModelVersion` → Hydration Job runs → FSx has the bytes.
2. **T1**: Deployment is updated with `MODEL_PATH=/fsx/...` and `Recreate` or `RollingUpdate maxSurge=1`.
3. **T2**: Karpenter provisions node → node registers → CSI mounts FSx (~10–20 s) → pod scheduled.
4. **T3**: `initContainer` optionally re-warms NVMe from FSx (30 GB/s).
5. **T4**: Main container starts; safetensors mmap opens files; first token in ~5–15 s.

### 8.1 Karpenter NodeClass tips for FSx-heavy workloads

- Use **taints** on GPU NodeClasses to prevent random workloads landing on them and hydrating pointless caches.
- Set `terminationGracePeriodSeconds` high enough that the pod can flush any local NVMe writeback.
- Pre-install the Lustre client in your custom EKS AMI; the `aws-fsx-csi-driver` Node plugin expects it. See [aws-fsx-csi-driver](https://github.com/kubernetes-sigs/aws-fsx-csi-driver).
- Use `spec.template.spec.nodeSelector` on the inference Deployment to pin to nodes in the same AZ as the FSx filesystem (`topology.kubernetes.io/zone: us-east-1a`).
- Karpenter's `consolidation` will happily terminate a node with a 200 GB warm NVMe cache. If that hurts, disable consolidation for the inference NodePool or use `Never` disruption policy.

### 8.2 Pre-scaling (warm pool) with Karpenter

Karpenter does not have a native warm pool primitive. Two ways to emulate:

- **Placeholder deployment**: a low-priority "pause" pod (`k8s.gcr.io/pause:3.9`) requesting the full GPU resource keeps `N` nodes up. Real workloads preempt via `priorityClassName` (higher priority + `preemptionPolicy: PreemptLowerPriority`). On preemption the pause pod is evicted, Karpenter scales a replacement.
- **`minValues` on NodeClaims** (Karpenter v1.0+): guarantees a floor of ready nodes per instance type.

Either way, the pause pod should also mount the FSx PVC, run an `initContainer` that pre-loads NVMe, and *then* sleep. That way when the real pod preempts it, the node's NVMe cache is warm and only the safetensors mmap needs to page in.

---

## 9. On the pod side — parallel loaders

Your storage substrate is only half the equation. The inference server has to actually consume the bytes.

### 9.1 safetensors + mmap

The [safetensors](https://huggingface.co/docs/safetensors/index) format is a header + tightly-packed tensor blobs. `safe_open("model.safetensors", framework="pt", device=0)` under the hood does:

1. `open(path, O_RDONLY)`
2. `read` the header (~KB)
3. `mmap(NULL, size, PROT_READ, MAP_SHARED, fd, 0)`
4. For each tensor, `torch.frombuffer(mmap_slice)` and `.to(device)` — this triggers `pread()` calls from Lustre + `cudaMemcpy` to HBM.

On a striped FSx file (`-c -1 -S 4M`), the OS issues many parallel `pread()`s that fan out across OSTs. This is why you specifically want striping and a bumped `max_rpcs_in_flight` — without it, the pipeline serializes.

If you can, keep tensors in the file in **the order the model consumes them** (which is what HuggingFace does by default for the sharded format). If you re-shard for tensor parallelism, keep this contiguity.

### 9.2 vLLM / TGI / SGLang loaders

- **vLLM**: uses HF's `from_pretrained`, which uses safetensors mmap. `VLLM_USE_MODELSCOPE=false`, `HF_HUB_OFFLINE=1`, and `MODEL_PATH=/fsx/weights/...` are the three env vars that get you the best cold-load path.
- **TGI**: same; add `--disable-custom-kernels` only if you hit issues; `MODEL_ID=/fsx/weights/...` or `HUGGINGFACE_HUB_CACHE=/fsx/hf-cache`.
- **SGLang**: `--model-path /fsx/weights/...`.

For tensor-parallel deployments (`--tensor-parallel-size 8`), each rank opens the same file(s) and reads its shard. With FSx `-c -1` striping this parallelizes across OSTs automatically. Do NOT split weights across per-rank subdirectories — you lose the shared-cache benefit.

### 9.3 PyTorch Distributed Checkpoint (DCP)

For training, [PyTorch DCP](https://docs.pytorch.org/docs/stable/distributed.checkpoint.html) writes one file per rank into a single directory:

```
/fsx/checkpoints/step-10000/
  __0_0.distcp
  __1_0.distcp
  ...
  __N_0.distcp
  .metadata
```

Each rank does independent `pwrite()`s. This is the ideal Lustre write pattern — every writer targets its own file, so contention is per-OST, not per-file. Set the parent directory to `lfs setstripe -c -1 -S 4M` and every checkpoint shard fans out.

Save-side timings on a well-tuned FSx PERSISTENT-1000 filesystem: a 405B-parameter DCP save (~1.6 TB across 256 ranks) completes in ~60 s at ~26 GB/s aggregate write.

### 9.4 Loading in parallel

If your loader doesn't already saturate the NIC, run multiple loader processes:

```python
from concurrent.futures import ThreadPoolExecutor
from safetensors import safe_open


def load_shard(path):
    with safe_open(path, framework="pt", device=0) as f:
        return {k: f.get_tensor(k) for k in f.keys()}


with ThreadPoolExecutor(max_workers=8) as ex:
    shards = list(ex.map(load_shard, shard_paths))
```

Eight parallel loaders × striped files ≈ 8 × OSTs of read parallelism, capped by NIC. On EFA-native this pins at 700 Gbps easily.

### 9.5 GDS path

With GDS + `libcufile`:

```python
import cupy
from cufile import CuFile

with CuFile("/fsx/weights/model.bin", "r") as cf:
    dev_buf = cupy.empty(size, dtype=cupy.uint8)
    cf.read(dev_buf)
```

This bypasses host DRAM entirely. Requires a P5 / P4d-class node with H100/A100, the Lustre client 2.15, and NVIDIA's `cufile` driver installed. See [FSx performance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html) for the supported combinations.

---

## 10. Comparison: FSx Lustre vs the alternatives, in depth

### 10.1 FSx Lustre vs S3 Mountpoint

**S3 Mountpoint** ([awslabs/mountpoint-s3](https://github.com/awslabs/mountpoint-s3)) is a FUSE client that translates POSIX read/write into S3 GET/PUT. Its sweet spot is:

- Read-heavy, sequential access to large objects.
- Write patterns limited to "create new object, close it" (no overwrite, no rename, no `pwrite`).
- Per-node local cache with `--cache /mnt/nvme`.
- Shared cache via S3 Express One Zone: `--cache-xz <express-bucket>` shards small (≤1 MB) objects.

Where it wins:
- **$/GB**: S3 Standard at $0.023 vs Lustre PERSISTENT-1000 at ~$0.53 is 23× cheaper.
- **Zero provisioning**: no throughput knob to size.
- **Multi-AZ durability** for the source of truth.

Where it loses:
- **Concurrent cold-scale**: N replicas × M shards = N×M GETs land on S3 at once. You'll hit per-prefix TPS ceilings on general-purpose buckets. Express One Zone helps but is still per-bucket-limited.
- **No POSIX writes**: rules out training checkpoint save (unless you write to Express and it's a fresh object each time).
- **Latency**: 40–150 ms first-byte on S3 Standard adds to every model shard open. For a 30-shard checkpoint that's 1–4 s of pure overhead per replica.
- **Per-node cache** invalidation: if two nodes cache different shards, you don't get filesystem-wide reuse.

**Decision rule**: if weights ≤ 20 GB, and you're OK with a few seconds of first-byte per pod on cold nodes, Mountpoint with a per-node NVMe cache is fine. Above that, Lustre wins on p99.

### 10.2 FSx Lustre vs S3 Express One Zone

**S3 Express One Zone** (directory buckets) is Amazon's answer to "S3 latency is too high for hot data." Key numbers:

- Single-digit millisecond first-byte.
- Up to 2M GET/s per bucket, 100k PUT/s per bucket ([directory bucket overview](https://docs.aws.amazon.com/AmazonS3/latest/userguide/directory-buckets-overview.html)).
- Single-AZ (you lose regional durability).
- Request cost ~1/8 of S3 Standard; storage cost ~7× more.

Where it fits for ML:
- Frequent small-object reads: LoRAs, tokenizer configs, embeddings.
- Mountpoint-S3 shared cache (`--cache-xz`) — coordinates 1 MB cache blocks across a fleet via Express.
- Weight loading when using Mountpoint on a single AZ. Cold-start p99 improves from S3 Standard's ~2 s per shard open to ~50 ms.

Where Lustre still beats it:
- Aggregate sustained throughput. Express does 2M req/s but each GET is bounded by object-server throughput; Lustre does 1200 Gbps *per client*.
- POSIX semantics (Lustre has real `pwrite`, rename, atomic ops; Express does not).
- Sharing partial-file caches across ranks (Lustre mmap wins).

### 10.3 FSx Lustre vs EFS

**EFS** is a regional multi-AZ NFS filesystem. Elastic Throughput mode delivers up to ~20 GB/s aggregate. But:
- Per-client throughput caps around 5 GB/s, roughly ¼ of Lustre EFA.
- No striping — a single 200 GB file is served from a single "block".
- Cost ~$0.30/GB-month plus provisioned throughput charges.
- POSIX-correct but higher latency (~1–5 ms).

EFS is *the* answer for:
- Shared home directories across a training cluster (dotfiles, Jupyter notebooks).
- Config that must survive AZ failure.
- Small-file writes where you want cross-AZ durability.

Not the answer for: model weight loading at scale, high-concurrency checkpointing.

### 10.4 FSx Lustre vs per-node EBS + prewarm

Baking weights into an EBS snapshot and attaching per-node is viable for small (<200 GB) models with rare updates:

- **Fast Snapshot Restore** on the snapshot eliminates lazy-load penalty.
- gp3 at 1 GB/s / io2 Block Express at 4 GB/s.
- Snapshot rehydrates in ~2–5 min for a 200 GB snapshot without FSR.

Downsides:
- New model version = new snapshot = orchestrate a rolling AMI or a new NodeClass.
- No shared filesystem for checkpointing.
- One-AZ pinning per volume (though snapshots are regional).

### 10.5 FSx Lustre vs NVMe instance store

NVMe is the fastest thing you have — 30 GB/s aggregate on `p5.48xlarge`. Its downside is that it's **ephemeral**: node terminate = data gone. Use it as a *cache in front of Lustre*, not as source of truth. See §7.5.

### 10.6 Composite: what a real inference cluster stores where

```
                                    S3 Standard (source of truth)
                                          │  daily sync via
                                          ▼
                                    FSx for Lustre
                                    │      │      │
                       ┌────────────┘      │      └────────────┐
                       │                   │                   │
                  weights/            checkpoints/        datasets/
                 (read-only)          (rw, DCP)          (read-mostly)
                       │                   │                   │
                       └───► per-node NVMe cache (opt) ◄───────┘
                                          │
                                          ▼
                                  vLLM / TGI / training job
                                          │
                                          ▼
                                       GPU HBM
```

And a parallel small-file / config path:

```
S3 Express One Zone (LoRAs, tokenizer configs, prompts)
              │
              ▼
Mountpoint S3 with --cache-xz (per pod)
```

---

## 11. Cost model — $/GB/month and $ per cold-start

### 11.1 Storage cost only

Take a Mixtral 8x22B fleet:
- 280 GB of weights.
- Kept on FSx PERSISTENT-500 (1000 MB/s baseline per TiB is overkill; 500 gives 500 MB/s/TiB baseline, 2400 GB minimum).
- 2.4 TiB × $0.29/GB-month = ~$700/month.
- S3 Standard copy: 280 GB × $0.023 = $6.44/month.
- Total: ~$706/month.

If you had 20 models at 280 GB each, still on the same 2.4 TiB filesystem: $700 + $130 = **$830/month** to serve 5.6 TB of hot weights at 1.2 GB/s aggregate baseline / 12 GB/s burst.

Comparable Mountpoint-only: 5.6 TB × $0.023 = **$129/month** but you eat per-request costs, cross-AZ transfer, and cold-start latency variance every scale-out.

### 11.2 Cost per cold-start

Simplified model: a single 140 GB Llama-70B replica cold-loads over N seconds.

| Substrate | Throughput per client | Wall-clock | $/hr GPU wasted at $30/hr p4d |
|---|---|---|---|
| S3 Mountpoint (no cache) | 4 GB/s (Mountpoint saturates ~40 Gbps sequential) | 35 s | $0.29 |
| S3 Express + Mountpoint | 8 GB/s | 17 s | $0.14 |
| FSx PERSISTENT-125 (1.2 TiB) | 150 MB/s (single OST cap) | 15 min | $7.50 |
| FSx PERSISTENT-500 (4.8 TiB, `-c -1`) | 2.4 GB/s (fs baseline) | 58 s | $0.48 |
| FSx PERSISTENT-1000 (4.8 TiB, `-c -1`) | 4.8 GB/s | 29 s | $0.24 |
| FSx PERSISTENT-1000 (12 TiB, EFA) | 12 GB/s | 12 s | $0.10 |
| FSx PERSISTENT-1000 + NVMe warm | 30 GB/s (NVMe read) | 5 s | $0.04 |
| GDS + FSx (700 Gbps) | 87 GB/s | 1.6 s | $0.014 |

The naive "small FSx to save money" bites you the moment your per-file stripe count is small — a single OST at 240 MB/s becomes the bottleneck.

### 11.3 Break-even vs Mountpoint

FSx pays off when:

`monthly_cold_starts × wasted_GPU_seconds × GPU_$/s > FSx_$/month − Mountpoint_$/month`

For a fleet doing 100 cold-starts/day on p4d ($30/hr = $0.0083/s), saving 30 s per cold-start yields $746/month. That's roughly the delta between the two configs. Anything more than ~100 cold-starts/day tips clearly to Lustre.

---

## 12. Checklist: optimizing p99 model-load latency

1. **Right substrate** — Lustre for shared weights above ~20 GB; Mountpoint+NVMe otherwise.
2. **Right AZ** — FSx and GPU nodes in the same AZ. Pin via `nodeSelector` / Karpenter requirements.
3. **Right PUST** — `capacity_TiB × PUST_MBps_per_TiB ≥ max_concurrent_replicas × weightsGB / target_seconds × 1.3`.
4. **Right striping** — `lfs setstripe -c -1 -S 4M` on model directories, *before* copying weights.
5. **Right client tuning** — `max_rpcs_in_flight`, `checksums=0`, `max_dirty_mb`, `noatime` mount. Baked into AMI, not runtime.
6. **Hydrate before scale-out** — one-shot DRA import task or hydration Job with a `.hydrated` sentinel.
7. **Idempotent initContainer** — every pod verifies FSx contents but does not re-download.
8. **NVMe cache tier** — for hot models on P5 nodes, mirror to `/nvme` for 30 GB/s serve.
9. **Safetensors + mmap** — never `torch.load(pickle)`. Prefer HF format.
10. **Parallel loaders** — 4–8 threads/processes per replica for tensor-parallel deployments.
11. **EFA / GDS** — enable on `p5.*` if PUST ≥ 10 GBps; requires custom AMI with `libcufile`.
12. **Karpenter warm pool** — pause pods that pre-mount FSx and pre-warm NVMe.
13. **Instrument p99** — Prometheus histogram over `pod_start_time - image_pulled_time` and `first_token_time - pod_ready_time`.
14. **CloudWatch on FSx** — watch `DataReadBytes`, `NetworkThroughputUtilization`, `IdleThroughputCapacity`, `DiskReadOperations`. If burst credits deplete you must bump PUST.
15. **Test the scale-out failure mode** — chaos-test 32 cold pod starts and confirm p99 pod-ready ≤ SLO.

## 13. Checklist: optimizing $/GB/month

1. **Right storage class** — Intelligent-Tiering for cold datasets; SSD PERSISTENT-125 for warm; PERSISTENT-1000 only where throughput demands it.
2. **LZ4 compression on** — `data_compression_type = LZ4`. Free on read, ~20% capacity savings on typical training data.
3. **DRA + S3 as cold tier** — release freed data via `RELEASE_DATA_FROM_FILESYSTEM` when a model retires.
4. **Right size**, then grow — FSx supports live storage capacity + throughput scale-up. Start smaller.
5. **Kill unused FSes** — one FS per team is easier than one per model. Consolidate.
6. **Metadata IOPS** — leave in Automatic unless you hit `MetadataOperations` throttling.
7. **No backups** — set `automatic_backup_retention_days = 0` for weights (S3 is the source of truth).
8. **Cross-account model registry** — one central S3 bucket, many per-region/per-AZ FSx replicas via DRA. Weights bucket bytes are cheap.
9. **Retire snapshots** — if you use FSx backups, purge aggressively.
10. **Right region** — FSx is not uniformly priced; some regions are notably more expensive.

---

## 14. Failure modes and gotchas

### 14.1 Silent under-striping

You created the directory *after* copying weights. Every existing file keeps its old layout. Symptom: `lfs getstripe` shows `stripe_count: 1` on 30 GB files. Fix: `lfs migrate -c -1 -S 4M -y <file>`.

### 14.2 One-shot DRA import limits

Only one DRA task runs at a time per filesystem. If you queue 8 imports, they serialize. Batch them by S3 prefix.

### 14.3 `PerUnitStorageThroughput` is a change-in-place

You can bump PUST live via `modify-file-system`, but the change takes ~6 hours to complete and consumes burst credits during the transition. Don't do it during peak load.

### 14.4 Karpenter consolidation kills your NVMe cache

By default, Karpenter tries to bin-pack. If a partially-utilized node has 200 GB of warm NVMe, Karpenter doesn't know or care. Options:
- `disruption.consolidateAfter: Never` on that NodePool.
- Taints/tolerations that keep the pause pod pinned.
- Annotate pods with `karpenter.sh/do-not-disrupt: "true"`.

### 14.5 `lfs setstripe -c -1` on a small filesystem

If FSx has 2 OSTs, `-c -1` == `-c 2`. Not a bug, but sizing matters. Aim for ≥ 8 OSTs (i.e., ≥ 9.6 TiB SSD).

### 14.6 EFA + Lustre client version mismatch

The Lustre 2.10 client on old EKS AMIs doesn't do EFA-native. You need 2.15 client + AL2023 or Ubuntu 22.04 with a 6.5+ kernel. This is silent — you just don't get EFA speeds.

### 14.7 Cross-AZ pod scheduling

An FSx filesystem lives in one subnet (one AZ). If a pod lands cross-AZ, the CSI driver still mounts it but every read goes cross-AZ at $0.01/GB *and* higher latency. Use `topology.kubernetes.io/zone` node selector on the Deployment.

### 14.8 Root cred required for `lctl set_param`

Client tuning requires `CAP_SYS_ADMIN`. Either bake into AMI at user-data time (running as root) or run a privileged DaemonSet. Do not try to `lctl` from an application pod.

### 14.9 Snapshot-based sharing across regions

You cannot share an FSx filesystem across regions. If you have a multi-region model serving deployment, you replicate S3 (cross-region replication) and stand up an FSx per region. The DRA connects each regional FSx to its regional bucket.

---

## 15. Reference: end-to-end EKS manifests

### 15.1 StorageClass (dynamic) — for scratch training FS

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: fsx-training-scratch
provisioner: fsx.csi.aws.com
parameters:
  subnetId: subnet-0abc...
  securityGroupIds: sg-0def...
  deploymentType: SCRATCH_2
  s3ImportPath: s3://datasets/imagenet-1k/
  s3ExportPath: s3://datasets-checkpoints/
mountOptions:
  - flock
reclaimPolicy: Delete
```

### 15.2 Static PV — for a pre-provisioned weights FS

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: fsx-weights
spec:
  capacity:
    storage: 4800Gi
  volumeMode: Filesystem
  accessModes: [ReadWriteMany]
  mountOptions: [noatime]
  persistentVolumeReclaimPolicy: Retain
  csi:
    driver: fsx.csi.aws.com
    volumeHandle: fs-0abc123::fsx::/weights   # filesystem id + mountname + subpath
    volumeAttributes:
      dnsname: fs-0abc123.fsx.us-east-1.amazonaws.com
      mountname: abc12bmv
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: fsx-weights
spec:
  accessModes: [ReadWriteMany]
  resources: { requests: { storage: 4800Gi } }
  volumeName: fsx-weights
  storageClassName: ""
```

### 15.3 Inference Deployment (vLLM, Llama-70B)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: llama3-70b-inference
spec:
  replicas: 8
  strategy:
    type: RollingUpdate
    rollingUpdate: { maxSurge: 2, maxUnavailable: 0 }
  selector: { matchLabels: { app: llama3-70b } }
  template:
    metadata:
      labels: { app: llama3-70b }
    spec:
      priorityClassName: inference-high
      nodeSelector:
        node.kubernetes.io/instance-type: p5.48xlarge
        topology.kubernetes.io/zone: us-east-1a
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
      volumes:
        - name: fsx-weights
          persistentVolumeClaim:
            claimName: fsx-weights
        - name: nvme
          hostPath:
            path: /mnt/nvme
            type: Directory
        - name: shm
          emptyDir:
            medium: Memory
            sizeLimit: 32Gi
      initContainers:
        - name: nvme-warm
          image: public.ecr.aws/aws-cli/aws-cli:2
          command:
            - /bin/bash
            - -c
            - |
              set -euo pipefail
              SRC=/fsx/weights/llama3-70b/v2.0
              DST=/nvme/llama3-70b/v2.0
              mkdir -p $DST
              if [[ -f $DST/.hydrated ]]; then echo "warm"; exit 0; fi
              cp -r $SRC/. $DST/
              touch $DST/.hydrated
          volumeMounts:
            - { name: fsx-weights, mountPath: /fsx/weights, readOnly: true }
            - { name: nvme,        mountPath: /nvme }
      containers:
        - name: vllm
          image: vllm/vllm-openai:v0.6.4
          args:
            - --model=/nvme/llama3-70b/v2.0
            - --tensor-parallel-size=8
            - --gpu-memory-utilization=0.92
            - --max-model-len=8192
            - --disable-log-requests
          env:
            - { name: HF_HUB_OFFLINE, value: "1" }
            - { name: TRANSFORMERS_OFFLINE, value: "1" }
          resources:
            limits:
              nvidia.com/gpu: 8
          readinessProbe:
            httpGet: { path: /health, port: 8000 }
            initialDelaySeconds: 30
            periodSeconds: 5
          volumeMounts:
            - { name: nvme, mountPath: /nvme, readOnly: true }
            - { name: shm,  mountPath: /dev/shm }
```

### 15.4 One-shot hydration Job (Pod Identity)

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: hydrate-llama3-70b-v2
  annotations:
    hydrate.example.com/model: llama3-70b
    hydrate.example.com/version: v2.0
spec:
  backoffLimit: 3
  activeDeadlineSeconds: 3600
  template:
    metadata:
      annotations:
        eks.amazonaws.com/skip-containers: ""
    spec:
      serviceAccountName: fsx-hydrator     # Pod Identity for fsx:*Task*, s3:ListBucket, s3:GetObject
      restartPolicy: OnFailure
      volumes:
        - name: fsx-weights
          persistentVolumeClaim: { claimName: fsx-weights }
      containers:
        - name: hydrator
          image: public.ecr.aws/aws-cli/aws-cli:2
          env:
            - { name: FS_ID,   value: fs-0abc123 }
            - { name: SRC_S3,  value: s3://models-bucket/llama3-70b/v2.0/ }
            - { name: DST_FS,  value: /fsx/weights/llama3-70b/v2.0 }
          command:
            - /bin/bash
            - -c
            - |
              set -euo pipefail
              mkdir -p "$DST_FS"
              lfs setstripe -c -1 -S 4M "$DST_FS" || true
              aws s3 sync --only-show-errors --cli-read-timeout 0 "$SRC_S3" "$DST_FS/"
              sync
              touch "$DST_FS/.hydrated"
          resources:
            requests: { cpu: "8", memory: "16Gi" }
            limits:   { cpu: "16", memory: "32Gi" }
          volumeMounts:
            - { name: fsx-weights, mountPath: /fsx/weights }
```

### 15.5 Pause pod for warm pool

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: llama3-70b-warm
spec:
  replicas: 2   # keep 2 warm GPU nodes always
  selector: { matchLabels: { app: warm-pool, model: llama3-70b } }
  template:
    metadata:
      labels: { app: warm-pool, model: llama3-70b }
      annotations:
        karpenter.sh/do-not-disrupt: "true"
    spec:
      priorityClassName: warm-pool-low   # lowest — inference preempts
      nodeSelector:
        node.kubernetes.io/instance-type: p5.48xlarge
        topology.kubernetes.io/zone: us-east-1a
      terminationGracePeriodSeconds: 5
      volumes:
        - name: fsx-weights
          persistentVolumeClaim: { claimName: fsx-weights }
        - name: nvme
          hostPath: { path: /mnt/nvme, type: DirectoryOrCreate }
      initContainers:
        - name: nvme-warm
          image: public.ecr.aws/aws-cli/aws-cli:2
          command: [/bin/bash, -c, |
              cp -r /fsx/weights/llama3-70b/v2.0/. /nvme/llama3-70b/v2.0/
              touch /nvme/llama3-70b/v2.0/.hydrated
            ]
          volumeMounts:
            - { name: fsx-weights, mountPath: /fsx/weights, readOnly: true }
            - { name: nvme,        mountPath: /nvme }
      containers:
        - name: pause
          image: registry.k8s.io/pause:3.9
          resources:
            requests: { nvidia.com/gpu: 8, cpu: "1", memory: "1Gi" }
            limits:   { nvidia.com/gpu: 8, cpu: "2", memory: "2Gi" }
```

### 15.6 Mountpoint-S3 for LoRAs on Express One Zone

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: loras-express
spec:
  capacity: { storage: 1200Gi }
  accessModes: [ReadOnlyMany]
  mountOptions:
    - allow-other
    - region us-east-1
    - cache /mnt/nvme/mp-cache
    - cache-xz loras--use1-az2--x-s3
    - metadata-ttl 60
  csi:
    driver: s3.csi.aws.com
    volumeHandle: loras-express
    volumeAttributes:
      bucketName: loras--use1-az2--x-s3
```

---

## 16. Training-specific patterns

### 16.1 Dataset staging

Training datasets are big (WebDataset shards, LAION-5B, RedPajama at ~5 TB) and read-mostly. Two patterns:

- **DRA lazy-load**: dataset lives in S3 Standard; DRA imports metadata; workers fault in data on demand.
- **Batch preload**: `aws fsx create-data-repository-task --type IMPORT_METADATA_FROM_REPOSITORY --paths s3://...` upfront.

For a distributed data-loader like `WebDataset` or `MosaicML streaming`, lazy is often fine — workers naturally spread reads across shards. But for step-count-stable epochs, batch preload eliminates first-epoch stragglers.

Stripe the dataset directory with the default PFL — mixed file sizes.

### 16.2 Checkpoint save

Use PyTorch DCP with FSx striped `-c -1`. Recommended:

```python
import torch.distributed.checkpoint as dcp

state_dict = {"model": model.state_dict(), "optim": optim.state_dict()}
dcp.save(
    state_dict=state_dict,
    storage_writer=dcp.FileSystemWriter("/fsx/checkpoints/step-10000", single_file_per_rank=True),
)
```

Checkpoint sizes (rough):

| Model | fp32 optim state | Total DCP save |
|---|---|---|
| Llama-3-8B | ~180 GB | ~200 GB |
| Llama-3-70B | ~1.4 TB | ~1.6 TB |
| Llama-3.1-405B | ~8 TB | ~9 TB |

At 12 GB/s aggregate write on FSx PERSISTENT-1000 12 TiB, a 405B DCP save runs in ~12 minutes. Save cadence directly limits step throughput — factor it in.

### 16.3 Checkpoint export

Round-trip trained models back to S3 asynchronously via DRA `auto_export_policy`:

```hcl
auto_export_policy { events = ["NEW", "CHANGED"] }
```

Every closed file exports to S3 automatically. Cost: standard S3 PUT price. Benefit: your checkpoints are durable to the same 11 nines as any S3 object, and can seed a new region.

### 16.4 Resumption

When resuming, PyTorch DCP reads each rank's file:

```python
dcp.load(
    state_dict=state_dict,
    storage_reader=dcp.FileSystemReader("/fsx/checkpoints/step-10000"),
)
```

Because each rank reads its own file from a striped directory, aggregate read throughput scales with rank count. On 256 ranks reading a 9 TB checkpoint: ~12 minutes if FSx has 12 GB/s baseline.

### 16.5 HyperPod integration

If you use SageMaker HyperPod for training and want the same weights available to an EKS inference cluster, mount the same FSx from both. HyperPod natively supports FSx mounts across the cluster — see the [HyperPod model deployment blog](https://aws.amazon.com/blogs/machine-learning/amazon-sagemaker-hyperpod-launches-model-deployments-to-accelerate-the-generative-ai-model-development-lifecycle/). Design pattern: HyperPod trains → writes checkpoint to `/fsx/checkpoints/step-N` → export policy fires → S3 has canonical copy → EKS inference cluster's DRA pulls latest.

---

## 17. Concrete numbers to remember

- **FSx SSD baseline**: 125 / 250 / 500 / 1000 MB/s per TiB (choose PUST).
- **FSx SSD burst**: 1300 MB/s per TiB, capped by network I/O credit accumulation.
- **FSx per-client**: 100 Gbps (non-EFA), 700 Gbps (EFA), 1200 Gbps (EFA + GDS).
- **FSx max DRAs per fs**: 8.
- **FSx max concurrent DRA tasks**: 1 per filesystem.
- **FSx metadata IOPS Automatic**: 1500 (1.2 TiB) → 12000 (48 TiB and up).
- **S3 Express**: 200k read TPS, 100k write TPS per bucket, ~5 ms first-byte.
- **S3 Standard**: 5500 GET TPS per prefix, ~50–150 ms first-byte.
- **Mountpoint-S3 shared cache limit**: 1 MB per cached object.
- **P5 NVMe**: 30 TB aggregate at ~30 GB/s.
- **P5 EFA**: 3200 Gbps aggregate.

## 18. Suggested experiments to run against your own cluster

1. **Baseline**: `dd if=/fsx/weights/testfile of=/dev/null bs=4M count=25600` (100 GB read) — record MB/s. Verify per-client throughput matches your NIC.
2. **Striping A/B**: same file with `-c 1` vs `-c -1`. Should be roughly OST-count× faster striped.
3. **Cold-start bake-off**: run 32 pod scale-out on Mountpoint (no cache), Mountpoint (Express), and FSx warm. Record `pod_ready_time` histograms.
4. **DCP save**: run a synthetic 500 GB DCP save at 16 ranks. Divide 500 GB by wall-clock; check against FSx aggregate baseline.
5. **Failure**: kill Lustre client mid-load, measure recovery. Verify `hard,intr` semantics.

---

## 19. References

- [Amazon FSx for Lustre performance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html)
- [FSx for Lustre performance tips (client tuning knobs)](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance-tips.html)
- [FSx for Lustre SSD storage class performance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/ssd-storage.html)
- [Data repository associations (DRAs)](https://docs.aws.amazon.com/fsx/latest/LustreGuide/create-dra-linked-data-repo.html)
- [Amazon FSx for Lustre CSI driver on EKS](https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi.html)
- [aws-fsx-csi-driver (source)](https://github.com/kubernetes-sigs/aws-fsx-csi-driver)
- [Mountpoint for Amazon S3](https://github.com/awslabs/mountpoint-s3)
- [Mountpoint for Amazon S3 CSI driver](https://github.com/awslabs/mountpoint-s3-csi-driver)
- [S3 Express One Zone directory bucket overview](https://docs.aws.amazon.com/AmazonS3/latest/userguide/directory-buckets-overview.html)
- [S3 Express One Zone product page](https://aws.amazon.com/s3/storage-classes/express-one-zone/)
- [SageMaker HyperPod inference operator launch](https://aws.amazon.com/blogs/machine-learning/amazon-sagemaker-hyperpod-launches-model-deployments-to-accelerate-the-generative-ai-model-development-lifecycle/)
- [awsome-distributed-training reference architectures](https://github.com/aws-samples/awsome-distributed-training)
- [AI on EKS blueprints](https://github.com/awslabs/ai-on-eks)
- [safetensors format](https://huggingface.co/docs/safetensors/index)
- [PyTorch Distributed Checkpoint](https://docs.pytorch.org/docs/stable/distributed.checkpoint.html)
- [Lustre.org manual — Managing File Layout (Striping) and Free Space](https://doc.lustre.org/lustre_manual.xhtml#managingstripingfreespace)

---

## Appendix A: quick recipes

### A.1 Preload a model into FSx, striped for max throughput

```bash
FS_MOUNT=/fsx
MODEL=llama3-70b
VER=v2.0

mkdir -p $FS_MOUNT/weights/$MODEL/$VER
lfs setstripe -c -1 -S 4M $FS_MOUNT/weights/$MODEL/$VER

aws s3 sync s3://models-bucket/$MODEL/$VER/ $FS_MOUNT/weights/$MODEL/$VER/

# verify
lfs getstripe $FS_MOUNT/weights/$MODEL/$VER/model-00001-of-00030.safetensors
```

### A.2 Client-side tuning (once per boot)

```bash
sudo tee /usr/local/sbin/lustre-tune.sh >/dev/null <<'EOF'
#!/usr/bin/env bash
set -e
lctl set_param osc.*.max_rpcs_in_flight=32
lctl set_param mdc.*.max_rpcs_in_flight=64
lctl set_param mdc.*.max_mod_rpcs_in_flight=50
lctl set_param llite.*.statahead_max=512
lctl set_param llite.*.statahead_agl=1
lctl set_param osc.*.checksums=0
lctl set_param osc.*.max_dirty_mb=512
lctl set_param llite.*.max_read_ahead_mb=512
EOF
sudo chmod +x /usr/local/sbin/lustre-tune.sh
```

### A.3 CloudWatch queries worth alerting on

- `AWS/FSx DataReadBytes Sum` per fs — 5-minute average close to `PUST × capacity_TiB × 1024 × 1024 × 5 × 60` is your ceiling. Alert at 80%.
- `AWS/FSx FreeDataStorageCapacity Minimum` — alarm below 20%.
- `AWS/FSx MetadataOperations Sum` — track against provisioned metadata IOPS.
- Sum of `DataReadBytes` divided by `PUST` gives you real-time throughput utilization percent.

### A.4 Reference architecture summary

```
┌──────────────────────────────────────────────────────────────────┐
│                          Region  us-east-1                       │
│                                                                   │
│   S3 Standard                     S3 Express (us-east-1a)         │
│   models-bucket                   loras--use1-az1--x-s3           │
│         │                                    │                    │
│         │ DRA import                         │ Mountpoint         │
│         ▼                                    │                    │
│   FSx PERSISTENT-1000, 4.8 TiB, us-east-1a   │                    │
│   /weights/*  /checkpoints/*  /datasets/*    │                    │
│         │                                    │                    │
│    ┌────┴────────┬────────────────┬──────────┴─────┐              │
│    ▼             ▼                ▼                ▼              │
│   Karpenter p5.48xlarge nodes (us-east-1a)                        │
│   ┌────────────────┐  ┌────────────────┐  ┌────────────────┐      │
│   │ inference pod  │  │ warm pool pod  │  │ training pod   │      │
│   │ - initContainer│  │ - pause        │  │ - torchrun DCP │      │
│   │ - vLLM         │  │ - pre-warm NVMe│  │ - safetensors  │      │
│   │ /nvme cache    │  │                │  │                │      │
│   └────────────────┘  └────────────────┘  └────────────────┘      │
│                                                                   │
└──────────────────────────────────────────────────────────────────┘
```
