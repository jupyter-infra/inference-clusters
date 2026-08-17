---
title: FSx for Lustre — S3 data repository associations (DRA), lazy loading, and export
slug: lustre-s3-drs
audience: infra / ML platform engineers
last_reviewed: 2026-08-06
---

# FSx for Lustre — S3 data repository associations (DRA), lazy loading, and export

## TL;DR

- An **FSx for Lustre data repository association (DRA)** is a 1:1 mapping between a directory on a Lustre file system and an S3 bucket-or-prefix. Up to **8 DRAs per file system**, no path overlap, and a DRA cannot span buckets — one DRA points at exactly one `s3://bucket/prefix/`. ([AWS docs](https://docs.aws.amazon.com/fsx/latest/LustreGuide/create-dra-linked-data-repo.html))
- **Auto-import** and **auto-export** policies are independent sets over the three event types `{NEW, CHANGED, DELETED}`. You can enable any combination on each side, including both sides simultaneously (bi-directional). Automatic sync is asynchronous and eventually-consistent. ([AWS docs — auto-import](https://docs.aws.amazon.com/fsx/latest/LustreGuide/autoimport-data-repo-dra.html), [AWS docs — auto-export](https://docs.aws.amazon.com/fsx/latest/LustreGuide/autoexport-data-repo-dra.html))
- **Lazy loading**: when an application first `read()`s a file whose S3 object is not yet in Lustre, FSx transparently pulls the object bytes from S3 through the OSTs on demand. Files exist as metadata-only inodes (Lustre HSM state `released exists archived`) until first access. ([AWS docs — importing changes](https://docs.aws.amazon.com/fsx/latest/LustreGuide/importing-files-dra.html))
- **Preload / hydrate** with `sudo lfs hsm_restore <path>` and confirm completion with `sudo lfs hsm_action <path>` returning `NOOP`. Bulk-hydrate a tree in parallel with `find | xargs -P N sudo lfs hsm_restore`. ([AWS docs — preload](https://docs.aws.amazon.com/fsx/latest/LustreGuide/preload-file-contents-hsm-dra.html))
- **Export back to S3** either via auto-export, `CreateDataRepositoryTask type=EXPORT_TO_REPOSITORY`, or per-file `sudo lfs hsm_archive`. All auto-exported objects land in **S3 Standard** class. ([AWS docs — export changes](https://docs.aws.amazon.com/fsx/latest/LustreGuide/export-changed-data-meta-dra.html), [AWS docs — HSM export](https://docs.aws.amazon.com/fsx/latest/LustreGuide/exporting-files-hsm.html))
- **POSIX metadata** (UID/GID, mode, mtime, atime, symlink target) round-trips through S3 as `x-amz-meta-file-*` object headers. Default mode for objects with no POSIX headers is **0755**. ([AWS docs — POSIX metadata](https://docs.aws.amazon.com/fsx/latest/LustreGuide/posix-metadata-support.html))
- **ML pattern**: keep source-of-truth weights and datasets in S3; on a training/inference cluster spin up (or reuse) a PERSISTENT_2 file system with a DRA whose auto-import fires but auto-export is deliberately off; `hsm_restore` the paths a job needs before compute lands. GPU nodes see local POSIX with sub-ms latency after hydration.

---

## 1. Scope and audience

This note is for infra and ML platform engineers who are wiring GPU-heavy Kubernetes or SLURM jobs to model weights and training data that already lives in S3. It covers the FSx for Lustre <-> S3 integration layer end-to-end: the DRA object model, the auto-import/auto-export event types, the underlying Lustre HSM state machine that makes lazy loading work, explicit hydration and export commands, POSIX <-> S3 metadata mapping, and cost/perf tradeoffs. It does not cover FSx for Lustre performance tuning in general, Intelligent-Tiering as a *storage class* (distinct from S3 IA/Glacier tiers), or File Cache (a separate service that reuses the same DRA plumbing).

Everything in this doc targets **Persistent 2** or **Persistent 1** file systems on Lustre 2.12/2.15. DRAs and auto-export are **not available** on Lustre 2.10 or `Scratch 1`. ([AWS docs — overview](https://docs.aws.amazon.com/fsx/latest/LustreGuide/overview-dra-data-repo.html))

---

## 2. The DRA object model

### 2.1 What a DRA actually is

A data repository association is a named, first-class resource (`dra-<hex>`) that pins:

1. A **file system path** on the FSx for Lustre file system — a directory (e.g. `/ns1/`, `/datasets/imagenet/`). The path must begin with `/`, and it must be unique among DRAs on that FS.
2. A **data repository path** — an S3 URL of the form `s3://bucket-name/optional/prefix/`. FSx will append a trailing `/` if you leave it off; `s3://amzn-s3-demo-bucket/foo` and `s3://amzn-s3-demo-bucket/foo/` are the same DRA target.
3. Auto-import and auto-export **event sets** (any combination of `NEW`, `CHANGED`, `DELETED`, or nothing at all on either side).
4. Optional import-on-create toggle (`BatchImportMetaDataOnCreate`) that kicks off an import task the moment the DRA reaches `AVAILABLE`.
5. Optional `ImportedFileChunkSize` (MiB, 1..512000, default 1024) — the Lustre stripe span used for large-object imports.

The DRA is created asynchronously; the API returns immediately with the DRA in `CREATING`, then it transitions to `AVAILABLE` (or `MISCONFIGURED` / `FAILED`). Documented lifecycle states: `CREATING`, `AVAILABLE`, `MISCONFIGURED`, `UPDATING`, `DELETING`, `FAILED`. ([AWS docs — create DRA](https://docs.aws.amazon.com/fsx/latest/LustreGuide/create-linked-dra.html), [AWS CLI reference](https://docs.aws.amazon.com/cli/latest/reference/fsx/create-data-repository-association.html))

### 2.2 The mapping model: 1:1 paths, no bucket-spanning

The file system path and the data repository path are a **1:1 mapping between paths in Amazon FSx and object keys in S3**. If the DRA is `/models/` <-> `s3://co-models/prod/`, then `/models/llama/7b/consolidated.pth` on the file system corresponds to `s3://co-models/prod/llama/7b/consolidated.pth`, byte for byte. ([AWS docs](https://docs.aws.amazon.com/fsx/latest/LustreGuide/create-dra-linked-data-repo.html))

Consequences:

- **A single DRA cannot span buckets.** If you have a dataset bucket and a checkpoints bucket, that's two DRAs and burns two of your eight-per-FS slots.
- **DRAs cannot overlap paths on either side.** If `/ns1` is linked, `/ns1/ns2` cannot be a separate DRA. Same on the S3 side: `s3://b/foo/` and `s3://b/foo/bar/` cannot coexist. This forces you to pick prefix boundaries carefully — the smart move is to plan bucket prefixes that mirror the directory layout you want in Lustre.
- **Only the first DRA on the FS can use `/` as its file system path.** After that, subsequent DRAs need real subdirectory paths. If you intend to have multiple DRAs, **do not** map `/` on the first one; give each DRA its own subtree from day one.

### 2.3 Bidirectional but not conflict-safe

You can turn on both auto-import (S3 -> Lustre) and auto-export (Lustre -> S3) on the same DRA. FSx will propagate in both directions asynchronously. **But** — and the docs are explicit about this — "it isn't guaranteed that a later edit in one location will overwrite an earlier edit in another location." If the same file is modified in both places, either can win. FSx does not implement a coordination layer; application-level coordination is required. ([AWS docs — auto-import](https://docs.aws.amazon.com/fsx/latest/LustreGuide/autoimport-data-repo-dra.html))

For inference clusters, this is not just theoretical. If you're pulling weights from S3 while a training pipeline is writing new checkpoints to the same DRA, the safer pattern is **one-way**: auto-import ON, auto-export OFF, and periodically flip the direction (e.g. weekly `EXPORT_TO_REPOSITORY` data repository tasks) to publish new artifacts.

### 2.4 Where auto-import can't help

Auto-import does **not** synchronize:

- S3 object lifecycle expirations
- Permanent deletion of the current object version in a versioning-enabled bucket
- Undeleting objects in a versioning-enabled bucket

So if your bucket uses lifecycle policies that transition objects to Glacier or delete them, don't expect Lustre to notice. Auto-import also gives up under two failure modes: missing bucket-level permissions (e.g. `s3:GetBucketAcl`) will push the DRA into `MISCONFIGURED`, and someone deleting the `FSx` event notification config on the bucket will do the same. ([AWS docs — auto-import](https://docs.aws.amazon.com/fsx/latest/LustreGuide/autoimport-data-repo-dra.html))

---

## 3. Auto-import policy: S3 -> Lustre

### 3.1 Event types

- **NEW** — S3 `PutObject`/`CompleteMultipartUpload` creates a fresh key that was not in Lustre. FSx creates the file's metadata (inode, size, POSIX mode, symlink target if it's a symlink) on the Lustre MDT. **The data is not pulled yet** — the file is `released exists archived`.
- **CHANGED** — an existing S3 key is overwritten. FSx invalidates the local file content and updates metadata. On the next read, the new content is pulled from S3. If a file already exists on Lustre and its S3 counterpart changes, **the local file is overwritten even if it's write-locked**. Anything a Lustre client wrote and never exported (see auto-export) is lost. That is by design; it's why you should not modify the same file in both locations.
- **DELETED** — S3 key removed. FSx deletes the corresponding file or the empty directory. If a Lustre directory is not empty, the directory is not removed.

You can pick any subset. The docs' explicit recommendation for "most use cases" is all three. ([AWS docs — auto-import](https://docs.aws.amazon.com/fsx/latest/LustreGuide/autoimport-data-repo-dra.html))

### 3.2 How it's wired under the hood

When auto-import is on, FSx installs an S3 event notification named literally `FSx` on the linked bucket. **Don't touch it.** Editing or deleting that notification config sends the DRA to `MISCONFIGURED`. ([AWS docs — auto-import](https://docs.aws.amazon.com/fsx/latest/LustreGuide/autoimport-data-repo-dra.html))

That also means: if you already have S3 event notifications on the bucket pointing at your own SQS or Lambda, FSx's DRA will still coexist with them (S3 supports multiple event notification configurations by name), but changes to any of them can cause drift. Terraform's `aws_s3_bucket_notification` is not per-name — if you re-apply a Terraform-managed notification block on that bucket, you will likely stomp on the `FSx` notification and push the DRA into `MISCONFIGURED`. **Guard your S3 buckets that participate in DRAs with lifecycle rules that exclude notification management from arbitrary IaC touches, or let FSx have exclusive ownership of the notification config.**

### 3.3 Region and account boundaries

- Auto-import **must be same-Region**: `us-east-1` bucket <-> `us-east-1` FSx. Cross-Region is not supported for auto-import. ([AWS docs — overview](https://docs.aws.amazon.com/fsx/latest/LustreGuide/overview-dra-data-repo.html))
- Auto-import **does support cross-Account**: bucket in account A, FSx in account B. Bucket policy needs to permit FSx service-linked role access.

### 3.4 Throughput ceiling and the `AgeOfOldestQueuedMessage` alarm

Auto-import is not free-lane. FSx documents a soft ceiling: on a single-shard bucket at the maximum change rate, auto-import will process about **7 hours of S3 backlog per 14 wall-clock days**. If your S3 change rate exceeds what auto-import can drain, the `AgeOfOldestQueuedMessage` CloudWatch metric grows. If it grows past **14 days**, FSx **stops auto-import** and marks the DRA `MISCONFIGURED`; recovering requires an `UpdateDataRepositoryAssociation` call and then a fresh import task for anything missed. Deletes that were missed cannot be reconciled by an import task — the docs bluntly note "you must re-create your file system" if you need a full re-sync including deletes. ([AWS docs — auto-import monitoring](https://docs.aws.amazon.com/fsx/latest/LustreGuide/autoimport-data-repo-dra.html))

There is also a per-file-system quota: **10 million file updates from linked S3 bucket per file system per month**. ([AWS docs — limits](https://docs.aws.amazon.com/fsx/latest/LustreGuide/limits.html))

**Set a CloudWatch alarm on `AgeOfOldestQueuedMessage` at 6h or 12h**. If your dataset ingest is bursty, throttle uploads to S3 or reshard so the notifications don't overrun the DRA.

### 3.5 What kinds of S3 changes are picked up

The auto-import documentation calls out:

- Changes to file **contents**
- Changes to file / directory **metadata**
- Changes to **symlink target** or symlink metadata
- **Deletions** of files and directories (only empty dirs)

Anything not on that list — POSIX ACLs, user-defined custom metadata, `setuid` — is not carried across. FSx only preserves the metadata fields it explicitly documents (see §7). ([AWS docs — POSIX metadata](https://docs.aws.amazon.com/fsx/latest/LustreGuide/posix-metadata-support.html))

### 3.6 Only POSIX-compliant keys

FSx only imports S3 objects "that have POSIX-compliant object keys," meaning keys that resemble a filesystem tree: `mydir/`, `mydir/myfile1`, `mydir/mysubdir/myfile2.txt`, etc. Directory sentinels are objects with keys ending in `/`. FSx does not synthesize implicit directories; if `mydir/mysubdir/myfile2.txt` exists but no `mydir/mysubdir/` object exists, FSx will still surface the file, but the parent directory's POSIX metadata will default to `0755`. ([AWS docs — POSIX metadata](https://docs.aws.amazon.com/fsx/latest/LustreGuide/posix-metadata-support.html))

---

## 4. Auto-export policy: Lustre -> S3

### 4.1 Event types (mirror image)

- **NEW** — a new file, directory, or symlink is created on Lustre. FSx PUTs a corresponding object to S3.
- **CHANGED** — an existing file's content or metadata changes. FSx **deletes the existing S3 object and creates a new one** with the current content and metadata. That means the ETag changes and the object's `LastModified` bumps. If you have consumers of S3 events downstream of this bucket, they will fire.
- **DELETED** — a file or directory is deleted on Lustre. FSx deletes the S3 key.

As with auto-import, any combination is legal. `NEW,CHANGED,DELETED` is the recommended full-sync policy. ([AWS docs — auto-export](https://docs.aws.amazon.com/fsx/latest/LustreGuide/autoexport-data-repo-dra.html))

### 4.2 When does export fire?

For content changes on a `CHANGED`-covered DRA, FSx exports **after the file is closed**. For metadata-only changes (chmod, chown, rename, timestamp changes other than `atime`/`mtime`), FSx exports immediately when the operation completes.

Two subtle behaviors:

- **`atime` and `mtime` alone do not trigger export.** They are synchronized on any export that fires for another reason. So a `touch` on a file that only mutates mtime won't cause an export; a chmod on the same file will, and the resulting S3 object will carry both the new mode and the new mtime.
- **`mv` always exports the target**, even if UID/GID/perm/content are unchanged. Renames in Lustre become delete-and-create in S3.

### 4.3 Region and account boundaries

- Auto-export **does support cross-Region**: FSx in `us-west-2`, bucket in `us-east-1` is fine.
- Auto-export **also supports cross-Account** exports. Bucket policy in the target account must allow the FSx service.

### 4.4 Storage class

**All objects created by auto-export or by export data repository tasks are written using the S3 Standard storage class.** ([AWS docs — auto-export](https://docs.aws.amazon.com/fsx/latest/LustreGuide/autoexport-data-repo-dra.html), [AWS docs — export tasks](https://docs.aws.amazon.com/fsx/latest/LustreGuide/export-changed-data-meta-dra.html))

If you want the exported objects to end up in S3 Intelligent-Tiering or S3 IA, you have two choices:

1. Configure S3 Lifecycle rules on the target bucket/prefix to transition auto-exported objects into the desired class after N days.
2. Point auto-export at a bucket with a **default storage class other than Standard** — S3 will still ignore the storage-class hint from FSx and honor its default only for Put; that's not how S3 works though, `PutObject` always writes to Standard unless the caller specifies a storage class header. FSx does not expose that header, so **option 1 (Lifecycle) is the only reliable path.**

### 4.5 What auto-export won't do

- If the S3 object already lives in **S3 Glacier Flexible Retrieval**, FSx will not sync `chmod`, `chown`, or `rename` on the Lustre file back to S3. Glacier objects are effectively frozen from FSx's point of view. ([AWS docs — export changes](https://docs.aws.amazon.com/fsx/latest/LustreGuide/export-changed-data-meta-dra.html))
- Auto-export only handles regular files, directories, and symlinks. FIFOs, block/char devices, sockets — never exported.
- S3 object keys have a **1024-byte** maximum. Files whose FSx-relative path exceeds 1024 UTF-8 bytes are silently skipped.
- FSx will not export non-UTF-8 filenames.

### 4.6 `AgeOfOldestQueuedMessage` and the "wait before delete" rule

Same metric applies to auto-export. If you ever plan to delete a DRA or the FSx file system, **wait for `AgeOfOldestQueuedMessage` to return to zero** before the delete, otherwise anything still queued will never reach S3. This one bites people. Add a check in your teardown automation that polls the metric before issuing `DeleteFileSystem`. ([AWS docs — auto-export monitoring](https://docs.aws.amazon.com/fsx/latest/LustreGuide/autoexport-data-repo-dra.html))

### 4.7 Simultaneous auto-export and export tasks are mutually exclusive

"Automatic export and export data repository tasks cannot run at the same time." If you're on auto-export and need a big one-shot flush, you have to disable auto-export first, run the task, then re-enable. Auto-import and import tasks, in contrast, **can** run simultaneously. ([AWS docs — export changes](https://docs.aws.amazon.com/fsx/latest/LustreGuide/export-changed-data-meta-dra.html))

---

## 5. Lazy loading: how first-touch actually works

### 5.1 The Lustre HSM model, applied to S3

FSx for Lustre implements S3 as an HSM (Hierarchical Storage Management) archive backend inside Lustre. Every file on the file system has an HSM state that describes where the "authoritative" bytes live:

| Flag | Meaning |
| --- | --- |
| `exists` | An archive copy is registered for this file (i.e. an S3 object exists / will exist). |
| `archived` | The archive copy is up to date with the file. |
| `dirty` | The file has been written on Lustre since the last successful archive; the S3 copy is stale. |
| `released` | The data blocks on Lustre have been released (evicted); only metadata + inode remain. Reads will re-hydrate. |
| `lost` | The archive can't be located. |

The concrete pattern for FSx-imported files: **`released exists archived`**, printed as `states: (0x00000009) exists archived` in `lfs hsm_state` output. That is what the docs call "the file has successfully been exported/imported" — the FS knows about the file, has metadata, and S3 has the canonical bytes. ([AWS docs — file release](https://docs.aws.amazon.com/fsx/latest/LustreGuide/file-release.html), [AWS docs — exporting via HSM](https://docs.aws.amazon.com/fsx/latest/LustreGuide/exporting-files-hsm.html))

### 5.2 The first read

When a client opens and reads a `released` file:

1. Lustre's HSM coordinator sees the file is `released` and issues an implicit restore.
2. FSx's copytool (managed by AWS, invisible to the user) issues one or more S3 `GetObject`s against the object.
3. Bytes stream into the OSTs. The application read syscall blocks until enough data is present to satisfy the offset.
4. On completion, the file state transitions to `exists archived` (no `released` flag), and future reads are pure Lustre I/O — sub-millisecond, no S3 involvement.

This is what "lazy loading" means: the first application touch is what pulls the data. Because it's transparent, jobs that page through a large dataset sequentially will slowly hydrate as they run. Random-access jobs (index lookups into a training shard) will spike S3 read requests unpredictably. For workloads that shouldn't take an S3 hit on the critical path, use **§6 explicit preloading**. ([AWS docs — importing changes](https://docs.aws.amazon.com/fsx/latest/LustreGuide/importing-files-dra.html))

### 5.3 Storage capacity semantics

You get all of the metadata but only as much *data* as fits in provisioned storage. From the docs: "If your linked S3 bucket is larger than your file system, you should be able to import all the file metadata into your file system. However, you can load only as much actual file data as will fit into the file system's remaining storage space. You'll receive an error if you attempt to access file data when there is no more storage left." ([AWS docs — preload](https://docs.aws.amazon.com/fsx/latest/LustreGuide/preload-file-contents-hsm-dra.html))

If you're operating close to capacity, use release tasks (§8) to evict cold files, and set alarms on `FreeDataStorageCapacity`.

### 5.4 The dirty state and export

When a Lustre client `write()`s an existing file, the state flips to `archived dirty exists`. Auto-export (if enabled) picks up the dirty flag, exports the file, and transitions back to `archived exists`. On file close, the export queue receives the event.

If auto-export is off, the file stays `dirty` forever unless something explicitly kicks off `hsm_archive` (either manually with `lfs hsm_archive`, or via an `EXPORT_TO_REPOSITORY` data repository task).

---

## 6. Preloading (hydration): `lfs hsm_restore`

### 6.1 Single-file restore

To force hydration of a specific path without going through the application read path:

```bash
sudo lfs hsm_restore /fsx/models/llama/7b/consolidated.pth
```

Note that `hsm_restore` returns almost immediately — it's a non-blocking submission of a restore request to the HSM coordinator. To know when the restore has actually completed:

```bash
sudo lfs hsm_action /fsx/models/llama/7b/consolidated.pth
# RESTORE     -> restore in progress
# NOOP        -> restore complete (or never needed)
```

`NOOP` means "no HSM action outstanding" — either the file was already fully local, or the restore just finished. To distinguish, run `lfs hsm_state`:

```bash
$ sudo lfs hsm_state /fsx/models/llama/7b/consolidated.pth
/fsx/models/llama/7b/consolidated.pth: (0x00000009) exists archived
```

The absence of `released` in the state flags means the file's data is resident on the OSTs. ([AWS docs — preload](https://docs.aws.amazon.com/fsx/latest/LustreGuide/preload-file-contents-hsm-dra.html))

### 6.2 Bulk parallel restore of a directory

The canonical pattern in the AWS docs is a `find | xargs -P N` fanout:

```bash
DIR=/fsx/datasets/imagenet
nohup find "$DIR" -type f -print0 \
  | xargs -0 -n 1 -P 8 sudo lfs hsm_restore &
```

`-P 8` sets the fanout. On a bigger EC2 host with more vCPUs you can go higher, but note two constraints:

- Each `hsm_restore` becomes a small submission to the HSM coordinator, not the S3 GET itself. The actual restore parallelism is bounded by the FSx-managed copytool and by S3 request rates.
- If you preload more data than fits on the FS, you'll trip the free-space error. Track `FreeDataStorageCapacity` while bulk-restoring.

### 6.3 Finding what's not yet hydrated

The AWS docs include a bash helper for enumerating released (not-yet-hydrated) files. Reproduced here:

```bash
#!/bin/bash
# Usage: find_lustre_released_files.sh /path/to/lustre/mount

ROOT_DIR="$1"
[ -d "$ROOT_DIR" ] || { echo "no such dir: $ROOT_DIR"; exit 1; }

THREADS=$(nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo)
OUTPUT_FILE="released_objects_$(date +%Y%m%d_%H%M%S).txt"

echo "Scanning $ROOT_DIR with $THREADS threads..."
time sudo lfs find "$ROOT_DIR" -type f \
  | parallel --will-cite -j "$THREADS" -n 1000 \
      "sudo lfs hsm_state {} | grep released" \
  > "$OUTPUT_FILE"

echo "Found $(wc -l <"$OUTPUT_FILE") released files, listed in $OUTPUT_FILE"
```

Prefer `lfs find` over GNU `find` for scanning Lustre trees — `lfs find` avoids the metadata round-trips that GNU `find` does with `stat`. ([AWS docs — preload](https://docs.aws.amazon.com/fsx/latest/LustreGuide/preload-file-contents-hsm-dra.html))

### 6.4 Reading the file also triggers hydration

If the workload doesn't care about the first-touch latency, you don't need `hsm_restore`. Just reading the file (e.g. `cat file > /dev/null`, `md5sum`, `python -c "open('/fsx/x').read()"`) causes the same restore path. The explicit-restore commands exist because:

1. You want to overlap hydration with something else (setup, container start, model compilation).
2. You want deterministic wall-clock behavior for a benchmark or a distributed sync.
3. You want to fail fast if S3 permissions are wrong, before the training job even starts.

### 6.5 Import tasks vs. hydration

An **import data repository task** (`aws fsx create-data-repository-task --type IMPORT_METADATA_FROM_REPOSITORY`) imports **metadata only**. It creates or updates inodes; it does *not* pull object bytes. The DRA-side "batch-import-meta-data-on-create" flag does the same thing, once, at DRA creation. That's a useful primitive for one-shot alignment of a Lustre FS with a bucket state that pre-dated the DRA. It's not a hydration mechanism.

```bash
aws fsx create-data-repository-task \
  --file-system-id fs-0123456789abcdef0 \
  --type IMPORT_METADATA_FROM_REPOSITORY \
  --paths s3://co-datasets/imagenet/ \
  --report Enabled=true,Path=s3://co-datasets/reports/,Format=REPORT_CSV_20191124,Scope=FAILED_FILES_ONLY
```

To hydrate the bytes, follow up the import task with an `hsm_restore` fanout. ([AWS docs — import task](https://docs.aws.amazon.com/fsx/latest/LustreGuide/import-data-repo-task-dra.html))

---

## 7. POSIX metadata mapping

### 7.1 The header contract

FSx serializes POSIX metadata as **S3 object user-metadata headers** on the exported object. The exact schema, from the docs ([AWS docs — POSIX metadata](https://docs.aws.amazon.com/fsx/latest/LustreGuide/posix-metadata-support.html)):

| S3 metadata header | POSIX meaning |
| --- | --- |
| `Content-Type` | HTTP entity media type (not really POSIX; used for browsers). |
| `x-amz-meta-file-permissions` | `<octal file type><octal permission mask>`, matching `st_mode` in [`stat(2)`](https://man7.org/linux/man-pages/man2/lstat.2.html). E.g. `0100664` = regular file + `rw-rw-r--`. `setuid` is not preserved. |
| `x-amz-meta-file-owner` | UID as integer. |
| `x-amz-meta-file-group` | GID as integer. |
| `x-amz-meta-file-atime` | Last-access time in nanoseconds since epoch, terminated with `ns`. Without the `ns` suffix, FSx interprets the value as milliseconds. |
| `x-amz-meta-file-mtime` | Last-modified time in nanoseconds since epoch, `ns`-terminated. |
| `x-amz-meta-user-agent` | On export, FSx sets this to `aws-fsx-lustre`. On import, ignored. |

Symlinks are represented as:

- **S3 object key** = the path to the symlink (relative to the FS mount).
- **S3 object body** = the *target path* of the symlink.
- **S3 object metadata** = the symlink's own POSIX metadata.

### 7.2 Uploading POSIX-tagged objects to S3

You can pre-tag S3 objects with POSIX metadata **before** creating the DRA, and FSx will honor those tags on import. This is critical for imports of pre-existing training data: without the tags, everything imports as UID 0 / mode 0755, which will break jobs that expect specific ownership.

Example from the docs:

```bash
# Create a directory sentinel object with POSIX metadata
aws s3api put-object \
  --bucket my-bucket \
  --key s3cptestdir/ \
  --metadata '{
    "user-agent":"aws-fsx-lustre",
    "file-atime":"1595002920000000000ns",
    "file-owner":"500",
    "file-permissions":"0100664",
    "file-group":"500",
    "file-mtime":"1595002920000000000ns"
  }'

# Upload a regular file with POSIX metadata
aws s3 cp s3cptestdir/s3cptest.txt s3://my-bucket/s3cptestdir/s3cptest.txt \
  --metadata '{
    "user-agent":"aws-fsx-lustre",
    "file-atime":"1595002920000000000ns",
    "file-owner":"500",
    "file-permissions":"0100664",
    "file-group":"500",
    "file-mtime":"1595002920000000000ns"
  }'
```

After DRA import, the file appears on Lustre with the mode/UID/GID/atime/mtime you tagged it with. ([AWS docs — attach POSIX perms](https://docs.aws.amazon.com/fsx/latest/LustreGuide/attach-s3-posix-permissions.html))

### 7.3 What is *not* carried

- **POSIX ACLs.**
- **User-defined custom metadata** not in the FSx schema.
- **`setuid` bits.**
- **Extended attributes** other than the FSx-recognized ones.

If your workload depends on any of those, you have to layer them on outside FSx.

### 7.4 The 0755 default

Objects that arrive at S3 with no `x-amz-meta-file-permissions` header are imported into Lustre as mode **0755** (`rwxr-xr-x`) owned by root:root. That's fine for public read-only datasets but wrong for anything the training container writes back to Lustre and expects to own. If your uploads originate from a tool that doesn't set the FSx metadata (`aws s3 cp`, `s5cmd`, `rclone`, `s3fs`, etc.), consider a small "POSIX-tag" script that walks the bucket and PUT-copies each object with the correct headers before you create the DRA.

---

## 8. Releasing (evicting) files: `lfs hsm_release` and release tasks

The complement of hydration. Releasing a file discards its Lustre data blocks and leaves the inode + metadata behind. Reads will re-hydrate from S3, transparently.

Prereqs, per docs:

- The file must have been exported to S3 already (state includes `archived`).
- You can release manually with `sudo lfs hsm_release <path>`.
- Or you can submit a **release data repository task** with a "minimum duration since last access" filter (in days). A value of `0` releases everything under the target path that has been exported. ([AWS docs — file release](https://docs.aws.amazon.com/fsx/latest/LustreGuide/file-release.html))

```bash
aws fsx create-data-repository-task \
  --file-system-id fs-0123456789abcdef0 \
  --type RELEASE_DATA_FROM_FILESYSTEM \
  --paths /fsx/datasets/imagenet \
  --release-configuration DurationSinceLastAccess='{Unit=DAYS,Value=7}' \
  --report Enabled=true,Path=s3://co-reports/,Format=REPORT_CSV_20191124,Scope=FAILED_FILES_ONLY
```

Release tasks are the right primitive for **automated cache eviction** on an FSx file system that fronts a big S3 dataset. Schedule one nightly via EventBridge Scheduler and you get a manageable working set without capacity blowouts. Do not use release tasks on files whose S3 counterparts are in Glacier — a subsequent read will fail because FSx does not restore Glacier-class objects on the fly (see §10.4).

---

## 9. Export back to S3

Three ways to move Lustre state back to S3, each with its own operational shape.

### 9.1 Auto-export (§4)

Best fit for continuous, real-time replication of a working directory back to S3, e.g. training checkpoints where you always want the freshest checkpoint object in S3. Cannot coexist with export tasks; imposes S3 Standard class.

### 9.2 Export data repository task (`EXPORT_TO_REPOSITORY`)

An on-demand batch operation. Only exports files created or modified since the last export. Supports up to 32 target paths in a single task and can emit a completion report (CSV) to a specified S3 prefix. ([AWS docs — export tasks](https://docs.aws.amazon.com/fsx/latest/LustreGuide/export-data-repo-task-dra.html))

```bash
aws fsx create-data-repository-task \
  --file-system-id fs-0123456789abcdef0 \
  --type EXPORT_TO_REPOSITORY \
  --paths checkpoints/run-123,logs/run-123 \
  --report Enabled=true,Path=s3://co-reports/,Format=REPORT_CSV_20191124,Scope=FAILED_FILES_ONLY
```

Paths are **relative to the mount** — no leading slash. Wildcards are not supported.

### 9.3 Per-file `lfs hsm_archive`

For fine-grained control from inside the file system:

```bash
sudo lfs hsm_archive /fsx/checkpoints/run-123/step-42.pt
sudo lfs hsm_state /fsx/checkpoints/run-123/step-42.pt
# expected: (0x00000009) exists archived
```

Bulk parallel export:

```bash
nohup find /fsx/checkpoints -type f -print0 \
  | xargs -0 -n 1 sudo lfs hsm_archive &
```

To count the outstanding not-yet-archived (or dirty) files:

```bash
find /fsx/checkpoints -type f -print0 \
  | xargs -0 -n 1 -P 8 sudo lfs hsm_state \
  | awk '!/\<archived\>/ || /\<dirty\>/' \
  | wc -l
```

Zero = full export achieved. ([AWS docs — exporting via HSM](https://docs.aws.amazon.com/fsx/latest/LustreGuide/exporting-files-hsm.html))

### 9.4 Object rewrite semantics

For `CHANGED` events (either auto-export or a task), FSx **deletes and re-creates** the S3 object. That has three implications:

- **ETag changes** on every content or metadata mutation.
- **S3 event notifications** downstream will fire twice conceptually — a delete then a create — though the API surface hides that. If you have a Lambda triggered by `s3:ObjectCreated:*` on the target bucket, expect two events per Lustre write.
- **Object versions**: on a versioning-enabled bucket, every export generates a new version and a delete marker for the previous. Storage cost can climb quickly. Consider a Lifecycle rule to expire noncurrent versions after N days.

---

## 10. Limits, quirks, and gotchas

### 10.1 Hard limits

- **8 DRAs per file system.** ([AWS docs — DRA overview](https://docs.aws.amazon.com/fsx/latest/LustreGuide/create-dra-linked-data-repo.html))
- **Only 1 DRA request worked on at a time** for a given FS; up to 8 can be queued.
- **10 million file updates per file system per month** on auto-import from S3. ([AWS docs — limits](https://docs.aws.amazon.com/fsx/latest/LustreGuide/limits.html))
- **S3 object key max 1024 bytes.** Longer paths are silently dropped on export.
- **`ImportedFileChunkSize`**: 1 to 512000 MiB, default 1024 (1 GiB). This controls Lustre striping for objects imported from S3; small values give more parallel-read potential across OSTs at the cost of more metadata overhead. For LLM weight shards (multi-GB single files), keep the default or set to a small multiple of the shard size.
- **Minimum FS storage capacity**: 1.2 TiB SSD, 6 TiB HDD. ([AWS docs — limits](https://docs.aws.amazon.com/fsx/latest/LustreGuide/limits.html))
- **Backups incompatible with DRAs.** Cannot enable file system backups on a file system with a linked DRA; must disable backups before linking, or maintain your source of truth in S3 (typical for DRA usage anyway).
- **Intelligent-Tiering FSx file systems** do not support DRAs. If you want the automatic Frequent/Infrequent/Archive tiering that FSx Intelligent-Tiering offers, you don't get S3 DRA on the same FS. ([AWS docs — data repositories](https://docs.aws.amazon.com/fsx/latest/LustreGuide/fsx-data-repositories.html))

### 10.2 Path constraints and depth

There is no documented maximum prefix depth, but the S3 object key limit of 1024 bytes bounds it in practice. FSx does not import objects whose key names are not POSIX-shaped (i.e. no bytes forbidden in a Linux filename, must be valid UTF-8). Objects whose keys resemble `//` (empty path components) will be skipped.

### 10.3 Large object counts vs. shards

FSx doesn't publish a hard "max number of objects per DRA," but S3 event notification throughput per bucket is bounded, and the DRA has one internal "shard" that consumes events. Under high change rates on a single bucket, `AgeOfOldestQueuedMessage` will grow (§3.4). If you're operating on datasets with hundreds of millions of small files and heavy churn, plan on:

- Multiple buckets with narrower prefixes, each linked as a separate DRA (up to 8).
- Or use `File Cache` (an adjacent AWS service that sits in front of multiple repositories and has its own DRA model, out of scope here).

### 10.4 Glacier gotchas

- FSx will **import metadata** for regular objects in S3 Glacier Flexible Retrieval or S3 Glacier Deep Archive, but **reads will fail** unless the object is first restored from Glacier via S3. ([AWS docs — importing changes](https://docs.aws.amazon.com/fsx/latest/LustreGuide/importing-files-dra.html))
- Symlinks in Glacier are **not** importable at all.
- Auto-export skips `chmod`/`chown`/`rename` sync when the target object is in Glacier.
- Release tasks on files whose S3 counterpart is in Glacier make those files effectively read-frozen from FSx's perspective.

Practical implication: **don't apply an S3 Lifecycle rule that tiers to Glacier on a bucket that FSx is reading from.** If your bucket must have Glacier for cost, keep the FSx-facing prefixes in Standard/Intelligent-Tiering.

### 10.5 Bidirectional conflicts

Reiterating from §2.3: **there is no CRDT or last-writer-wins semantics** on same-file conflicts. Anything relying on both directions being synced against the same key is asking for corruption.

### 10.6 `Scratch 1` and Lustre 2.10

DRAs, auto-export, and multiple data repositories are unavailable on `Scratch 1` and Lustre 2.10 file systems. New file systems default to Lustre 2.15; you can force 2.12 or `Scratch 2` for compat. Nothing net-new should be built on 2.10.

### 10.7 Auto-import backlog is fragile

The 14-day cap on `AgeOfOldestQueuedMessage` is a hard failure mode. In production, alarm at 6h or 12h and page. Once auto-import is `MISCONFIGURED`, a full re-sync (including deletes) requires **file system recreation**. The remediation path is:

1. `UpdateDataRepositoryAssociation` (any change; the docs say "The only request parameter that you need is the AssociationID") to return the DRA to `AVAILABLE`.
2. Run an `IMPORT_METADATA_FROM_REPOSITORY` task to catch up creates/modifies.
3. Accept that S3 deletes during the outage window will not propagate. If that's not acceptable, teardown + rebuild.

### 10.8 The `FSx` S3 event-notification config

FSx installs an event notification configuration named `FSx` on every S3 bucket linked via a DRA with auto-import enabled. Any tool that manages S3 event notifications with a "replace-all" semantic (Terraform's `aws_s3_bucket_notification` is one) can inadvertently overwrite it. Best practice:

- Give FSx exclusive ownership of S3 event notifications on that bucket, or
- Manage the bucket's notification config via CloudFormation with `NotificationConfiguration.QueueConfigurations` etc. explicitly listed alongside a rule for FSx (not currently first-class in Terraform).
- Monitor DRA lifecycle for transitions to `MISCONFIGURED` in CloudWatch.

---

## 11. IaC: Terraform DRA resource

The Terraform AWS provider exposes `aws_fsx_data_repository_association`. Full HCL example:

```hcl
resource "aws_fsx_lustre_file_system" "inference" {
  storage_capacity            = 4800    # TiB, must be multiple of 2.4 for Persistent 2
  subnet_ids                  = [var.private_subnet_id]
  deployment_type             = "PERSISTENT_2"
  per_unit_storage_throughput = 500     # MB/s per TiB
  security_group_ids          = [aws_security_group.fsx.id]

  tags = {
    Name = "inference-fsx"
  }
}

resource "aws_fsx_data_repository_association" "weights" {
  file_system_id       = aws_fsx_lustre_file_system.inference.id
  data_repository_path = "s3://co-model-weights/prod/"
  file_system_path     = "/weights/"

  batch_import_meta_data_on_create = true
  imported_file_chunk_size         = 1024   # MiB per stripe

  s3 {
    auto_import_policy {
      events = ["NEW", "CHANGED", "DELETED"]
    }

    # Weights bucket is source of truth; no export needed.
    # Omit or leave events empty to disable auto-export.
    auto_export_policy {
      events = []
    }
  }

  tags = {
    Environment = "prod"
    Purpose     = "model-weights"
  }
}

resource "aws_fsx_data_repository_association" "checkpoints" {
  file_system_id       = aws_fsx_lustre_file_system.inference.id
  data_repository_path = "s3://co-training-checkpoints/prod/"
  file_system_path     = "/checkpoints/"

  s3 {
    # Checkpoints go one-way: FS -> S3.
    auto_import_policy {
      events = []
    }
    auto_export_policy {
      events = ["NEW", "CHANGED", "DELETED"]
    }
  }
}
```

Notes:

- `imported_file_chunk_size` is expressed in MiB (not bytes). Docs cap at 512000.
- `file_system_path` is required for anything beyond the first DRA and effectively required in practice — plan for it always.
- `batch_import_meta_data_on_create` fires exactly once at creation, then never again. Cannot be re-toggled.

CloudFormation and CDK have equivalent resources (`AWS::FSx::DataRepositoryAssociation`); parameters are the same names in PascalCase.

---

## 12. Kubernetes/EKS integration

The FSx for Lustre CSI driver ([github.com/kubernetes-sigs/aws-fsx-csi-driver](https://github.com/kubernetes-sigs/aws-fsx-csi-driver)) can dynamically provision an FS with an `AutoImportPolicy` via the storage class, or you can pre-provision the FS with Terraform (as above) and reference it as a static PV.

Static PV example, referencing a pre-existing FS with two DRAs baked in:

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: inference-fsx-pv
spec:
  capacity:
    storage: 4800Ti
  volumeMode: Filesystem
  accessModes:
    - ReadWriteMany
  mountOptions:
    - flock
  persistentVolumeReclaimPolicy: Retain
  csi:
    driver: fsx.csi.aws.com
    volumeHandle: fs-0123456789abcdef0
    volumeAttributes:
      dnsname: fs-0123456789abcdef0.fsx.us-east-1.amazonaws.com
      mountname: abc123
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: inference-fsx-pvc
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: ""
  resources:
    requests:
      storage: 4800Ti
  volumeName: inference-fsx-pv
```

Once mounted, `/mnt/fsx/weights/` and `/mnt/fsx/checkpoints/` are visible as normal POSIX trees. GPU pods that read from `/mnt/fsx/weights/llama/7b/consolidated.pth` trigger lazy-load on first access.

For deterministic warm-up before the training pod runs, use an **init container** that runs `lfs hsm_restore` against the paths the workload needs:

```yaml
initContainers:
  - name: warm-weights
    image: <image with lfs-utils>
    securityContext:
      privileged: true    # required for lfs hsm_* commands
    command:
      - /bin/bash
      - -c
      - |
        set -euo pipefail
        find /mnt/fsx/weights/llama/7b -type f -print0 \
          | xargs -0 -n 1 -P 32 lfs hsm_restore
        # wait until nothing is 'released' anymore
        while lfs find /mnt/fsx/weights/llama/7b -type f \
               | xargs -n 1000 lfs hsm_state \
               | grep -q released; do
          sleep 5
        done
    volumeMounts:
      - name: fsx
        mountPath: /mnt/fsx
```

Not all container images ship `lfs`; you'll want a small `lustre-client` image. Amazon's own Lustre-client packages are documented at [FSx for Lustre — mounting the FS](https://docs.aws.amazon.com/fsx/latest/LustreGuide/mounting-from-ec2-instance.html) (or via `lustre-client` in the Amazon Linux 2 extras / DKMS on other distros).

---

## 13. The ML pattern: weights + datasets in S3, hydrate on demand

### 13.1 Layout

Recommended layout for an inference or training cluster:

```
s3://co-model-weights/                  # source of truth, one prefix per model family
  prod/
    llama/
      7b/
        consolidated.pth
        params.json
        tokenizer.model
      70b/
        ...
    mixtral/
      8x7b/
        ...

s3://co-training-datasets/              # source of truth, dataset-by-dataset
  imagenet/
    train/000000.tar
    train/000001.tar
    ...
    val/...

s3://co-training-checkpoints/           # one prefix per run
  runs/
    2026-07-15-llama-7b-instruct/
      step-00001.pt
      step-00002.pt
      ...
```

### 13.2 DRA wiring

- **Weights DRA**: `s3://co-model-weights/prod/` <-> `/weights/`, auto-import `NEW,CHANGED,DELETED`, auto-export empty. Weights change rarely and always outside the cluster.
- **Datasets DRA**: `s3://co-training-datasets/` <-> `/datasets/`, auto-import `NEW,CHANGED,DELETED`, auto-export empty. Datasets are read-only inputs.
- **Checkpoints DRA**: `s3://co-training-checkpoints/runs/` <-> `/checkpoints/`, auto-import empty, auto-export `NEW,CHANGED,DELETED`. FS is the working store; S3 is the durable copy.

That's three DRAs out of eight, leaving room for logs, scratch, etc.

### 13.3 Warm-up strategy

Two patterns depending on job characteristics:

**Pattern A — per-job hydrate on schedule.** For a training job that always uses the same model family + dataset, run an init container that `hsm_restore`s exactly the paths that job needs. Fast, deterministic, and doesn't hydrate stuff you don't need. Downside: cold on first run; nodes evict data over time.

**Pattern B — pre-hydrate at cluster provision.** For an inference fleet that always serves the same 3-4 models, hydrate all model weights at FS provision time, then use release tasks to evict long-idle files. `hsm_restore` is idempotent; there's no cost to running it on already-restored files (returns without action).

### 13.4 Reads from GPU nodes

Once files are `archived` (data resident on OSTs), reads from GPU nodes are pure Lustre I/O, and the FSx OSS network path targets **hundreds of GBps** aggregate on Persistent 2 EFA-enabled file systems (1000 MBps/TiB * FS size). A 4800 TiB Persistent 2 FS with 500 MBps/TiB gets you 2.4 TB/s aggregate read bandwidth across all clients, spread across ~500 OSSes — plenty for a training run that streams weight shards in parallel across dozens of ranks. ([AWS docs — deployment types](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html))

### 13.5 Checkpoint export cadence

Auto-export publishes every checkpoint on file close, so a training run that writes `step-N.pt` gets each shard PUT to S3 as soon as it's written. Two operational things to watch:

- **`AgeOfOldestQueuedMessage`** on the checkpoints DRA — if training checkpointing is faster than auto-export can flush, watch it grow.
- **Versioning + Lifecycle**: enable versioning on the checkpoints bucket and add a Lifecycle rule to expire noncurrent versions after ~30 days. Auto-export's delete-and-recreate on `CHANGED` will otherwise pile up versions.

Alternate pattern: turn off auto-export and run an `EXPORT_TO_REPOSITORY` task every N steps or at end-of-epoch. Cheaper on S3 requests, less real-time.

### 13.6 Data locality vs. cost

FSx for Lustre is not cheap: at 500 MBps/TiB Persistent 2, list price is roughly $0.145/GB-month for SSD (list; region-dependent, check [FSx pricing](https://aws.amazon.com/fsx/lustre/pricing/)). Every TiB you provision costs. The pattern that works:

- Provision FS large enough for **the working set**, not the entire dataset.
- Rely on lazy loading + release tasks to keep the working set fresh.
- Source of truth is S3 (much cheaper per GB, effectively unbounded).

Rule of thumb: for a 100 TB dataset and a job that touches ~10 TB per run, a 20-25 TiB FSx FS with aggressive release-task eviction is usually enough.

---

## 14. Monitoring the DRA

CloudWatch metrics that matter for a DRA workflow ([AWS docs — CW metrics](https://docs.aws.amazon.com/fsx/latest/LustreGuide/monitoring-cloudwatch.html)):

| Metric | Meaning | Alarm suggestion |
| --- | --- | --- |
| `AgeOfOldestQueuedMessage` | Age of the oldest queued auto-import or auto-export event. | > 6h WARN, > 12h PAGE. |
| `FreeDataStorageCapacity` | Free bytes on the FS. | < 10% PAGE (also cued by release-task cadence). |
| `DataReadBytes` / `DataWriteBytes` | Aggregate throughput to/from clients. | Trend only. |
| `FilesLostFromRepoNotifications` | (Sparse) count of S3 events that couldn't be processed. | Any non-zero PAGE. |
| DRA lifecycle | Not a metric; check via `DescribeDataRepositoryAssociations`. | Any `MISCONFIGURED` PAGE. |

Turn on **CloudWatch Logs data-repository event logging** at FS creation. Warning/error events include the S3 key that failed and the reason (permissions, encryption, etc.). Without the log, debugging a stuck import is guesswork. ([AWS docs — data-repo event logs](https://docs.aws.amazon.com/fsx/latest/LustreGuide/data-repo-event-logs.html))

---

## 15. Encryption and permissions

### 15.1 SSE-S3 vs. SSE-KMS

DRAs work with S3 buckets that use SSE-S3 or SSE-KMS. For SSE-KMS, the FSx service-linked role (or the role FSx assumes for that DRA) needs `kms:Decrypt` on the bucket's KMS key. If the bucket key is a customer-managed KMS key in a different account, cross-account grants are required. ([AWS docs — SSE bucket support](https://docs.aws.amazon.com/fsx/latest/LustreGuide/s3-server-side-encryption-support.html))

### 15.2 IAM permissions on the bucket

At minimum, FSx needs on the bucket:

- `s3:GetBucketAcl`, `s3:GetBucketPolicy`, `s3:GetBucketNotification`, `s3:PutBucketNotification` (to install the `FSx` event config).
- `s3:GetObject`, `s3:GetObjectAcl`, `s3:GetObjectVersion` (for import).
- `s3:PutObject`, `s3:DeleteObject`, `s3:AbortMultipartUpload` (for export).
- `s3:ListBucket`.

Missing bucket-level perms will drop the DRA into `MISCONFIGURED` and stop everything.

### 15.3 The service-linked role

FSx uses `AWSServiceRoleForAmazonFSx`. AWS auto-creates it the first time you create a file system in the account. For cross-account DRAs, the target bucket policy must reference the FSx service principal (`fsx.amazonaws.com`), not the account you launched the FS in.

---

## 16. Comparison to alternatives

Not exhaustive, but where DRA fits vs. neighbors:

| Approach | Latency to first byte | Throughput to GPUs | POSIX? | Cost profile |
| --- | --- | --- | --- | --- |
| Read directly from S3 (boto3, s5cmd) | Tens of ms per GET | Bounded by S3 GET request rate per prefix (~5.5k/s) | No | Cheapest; S3 request cost dominates. |
| Mountpoint for S3 / s3fs | Tens of ms per GET; some caching | Same S3 ceiling | Read-mostly POSIX; write-back is awkward | Cheap; adds a client daemon. |
| FSx for Lustre + DRA (lazy load) | ~sub-ms after hydration | Hundreds of GBps aggregate on Persistent 2 EFA | Yes | Pricey per GB-month; amortize by keeping working set small. |
| EFS + S3 sync (aws s3 sync) | ms; NFS latency | ~10s of GBps aggregate | Yes | Middle of the road; less bandwidth than Lustre. |
| Local NVMe on each node + S3 sync | Sub-ms | Per-node NVMe bandwidth | Yes | Cheap; no sharing across nodes. |

For inference clusters at multi-GPU scale where model weights are shared across pods and each pod reads a lot in parallel, **FSx for Lustre + DRA is the pattern that gives you both sub-ms latency and cross-node sharing**. It's not for small datasets or single-node fine-tunes — Mountpoint or local NVMe is better in those cases.

---

## 17. Anti-patterns and lessons learned

- **Enabling both `AutoImport` and `AutoExport` on the same DRA in production without careful path partitioning.** You will eventually see a conflict; the docs are clear that either side can win. Only do this on a scratch scratch directory that no other producer touches.
- **Deleting the file system to force a re-sync.** The docs suggest this as the remediation for a `MISCONFIGURED` DRA with backlog > 14 days. Do not treat this as a normal operation. Guard against it with the 6h alarm.
- **Using `aws s3 cp` without `--metadata` for bulk uploads intended for FSx import.** Everything imports as UID 0 / mode 0755. Root-only. If your GPU pod runs as UID 1000, it can read but not write anything. Fix: tag objects with FSx POSIX headers at upload time, or run a POSIX-tagging job before the DRA is created.
- **Managing the linked bucket's S3 event notifications with Terraform.** Will stomp on the FSx notification config. Move the notification config out of Terraform or ensure FSx is the sole owner.
- **Placing the linked bucket behind a Lifecycle rule that transitions to Glacier.** Reads through Lustre will fail for those objects; FSx will not restore Glacier objects transparently.
- **Ignoring `AgeOfOldestQueuedMessage`.** In every DRA incident, the first thing to check.
- **Assuming `hsm_restore` is synchronous.** It's a submission. Use `hsm_action` = `NOOP` and `hsm_state` without `released` as the completion signal.
- **Forgetting that auto-export re-creates the S3 object** on every content or metadata mutation. Versioning + naive Lifecycle can rack up costs quickly.
- **Using FSx Intelligent-Tiering FS for a workflow that expects DRA.** Intelligent-Tiering FS does not support DRA; you'd need a Persistent 2 SSD FS instead (and manage tiering via S3 Lifecycle on the source bucket, not on the FS).

---

## 18. Reference commands

### 18.1 Create a DRA with both auto-import and auto-export

```bash
aws fsx create-data-repository-association \
  --file-system-id fs-0123456789abcdef0 \
  --file-system-path /datasets/ \
  --data-repository-path s3://co-training-datasets/ \
  --batch-import-meta-data-on-create \
  --imported-file-chunk-size 1024 \
  --s3 '{
    "AutoImportPolicy":{"Events":["NEW","CHANGED","DELETED"]},
    "AutoExportPolicy":{"Events":[]}
  }' \
  --tags Key=Purpose,Value=datasets
```

### 18.2 Force an import task on a subtree

```bash
aws fsx create-data-repository-task \
  --file-system-id fs-0123456789abcdef0 \
  --type IMPORT_METADATA_FROM_REPOSITORY \
  --paths s3://co-training-datasets/imagenet/ \
  --report Enabled=true,Path=s3://co-reports/imagenet/,Format=REPORT_CSV_20191124,Scope=FAILED_FILES_ONLY
```

### 18.3 Force an export task on a subtree

```bash
aws fsx create-data-repository-task \
  --file-system-id fs-0123456789abcdef0 \
  --type EXPORT_TO_REPOSITORY \
  --paths checkpoints/run-123 \
  --report Enabled=true,Path=s3://co-reports/,Format=REPORT_CSV_20191124,Scope=FAILED_FILES_ONLY
```

### 18.4 Release everything older than 7 days

```bash
aws fsx create-data-repository-task \
  --file-system-id fs-0123456789abcdef0 \
  --type RELEASE_DATA_FROM_FILESYSTEM \
  --paths /fsx/datasets/imagenet \
  --release-configuration DurationSinceLastAccess='{Unit=DAYS,Value=7}' \
  --report Enabled=true,Path=s3://co-reports/,Format=REPORT_CSV_20191124,Scope=FAILED_FILES_ONLY
```

### 18.5 Update DRA policies

```bash
aws fsx update-data-repository-association \
  --association-id dra-0123456789abcdef0 \
  --s3 '{
    "AutoImportPolicy":{"Events":["NEW","CHANGED"]},
    "AutoExportPolicy":{"Events":["NEW","CHANGED","DELETED"]}
  }'
```

### 18.6 Check HSM state on a file

```bash
sudo lfs hsm_state /fsx/weights/llama/7b/consolidated.pth
# /fsx/weights/llama/7b/consolidated.pth: (0x00000009) exists archived    <- data on FS, S3 in sync
# /fsx/weights/llama/7b/consolidated.pth: (0x0000000d) released exists archived  <- metadata only
# /fsx/weights/llama/7b/consolidated.pth: (0x00000003) exists dirty       <- FS modified, S3 stale
```

### 18.7 Preload a directory in parallel

```bash
DIR=/fsx/weights/llama/7b
nohup find "$DIR" -type f -print0 \
  | xargs -0 -n 1 -P 16 sudo lfs hsm_restore &
```

### 18.8 Wait for a preload to finish

```bash
DIR=/fsx/weights/llama/7b
while true; do
  REMAINING=$(sudo lfs find "$DIR" -type f \
    | xargs -n 1000 sudo lfs hsm_state \
    | grep -c 'released')
  echo "still released: $REMAINING"
  [ "$REMAINING" -eq 0 ] && break
  sleep 5
done
```

### 18.9 Export a single file manually

```bash
sudo lfs hsm_archive /fsx/checkpoints/run-123/step-42.pt
sudo lfs hsm_state /fsx/checkpoints/run-123/step-42.pt
```

### 18.10 Query DRA state

```bash
aws fsx describe-data-repository-associations \
  --association-ids dra-0123456789abcdef0 \
  --query 'Associations[0].[Lifecycle,FailureDetails.Message,S3.AutoImportPolicy,S3.AutoExportPolicy]'
```

---

## 19. References

### AWS documentation

- FSx for Lustre — Using data repositories: [`fsx-data-repositories.html`](https://docs.aws.amazon.com/fsx/latest/LustreGuide/fsx-data-repositories.html)
- Overview of data repositories: [`overview-dra-data-repo.html`](https://docs.aws.amazon.com/fsx/latest/LustreGuide/overview-dra-data-repo.html)
- Linking your file system to an Amazon S3 bucket: [`create-dra-linked-data-repo.html`](https://docs.aws.amazon.com/fsx/latest/LustreGuide/create-dra-linked-data-repo.html)
- Creating a link to an S3 bucket: [`create-linked-dra.html`](https://docs.aws.amazon.com/fsx/latest/LustreGuide/create-linked-dra.html)
- Automatically import updates from your S3 bucket: [`autoimport-data-repo-dra.html`](https://docs.aws.amazon.com/fsx/latest/LustreGuide/autoimport-data-repo-dra.html)
- Automatically export updates to your S3 bucket: [`autoexport-data-repo-dra.html`](https://docs.aws.amazon.com/fsx/latest/LustreGuide/autoexport-data-repo-dra.html)
- Importing changes from your data repository: [`importing-files-dra.html`](https://docs.aws.amazon.com/fsx/latest/LustreGuide/importing-files-dra.html)
- Preloading files into your file system: [`preload-file-contents-hsm-dra.html`](https://docs.aws.amazon.com/fsx/latest/LustreGuide/preload-file-contents-hsm-dra.html)
- Exporting changes to the data repository: [`export-changed-data-meta-dra.html`](https://docs.aws.amazon.com/fsx/latest/LustreGuide/export-changed-data-meta-dra.html)
- Exporting files using HSM commands: [`exporting-files-hsm.html`](https://docs.aws.amazon.com/fsx/latest/LustreGuide/exporting-files-hsm.html)
- Using data repository tasks to export changes: [`export-data-repo-task-dra.html`](https://docs.aws.amazon.com/fsx/latest/LustreGuide/export-data-repo-task-dra.html)
- Using data repository tasks to import changes: [`import-data-repo-task-dra.html`](https://docs.aws.amazon.com/fsx/latest/LustreGuide/import-data-repo-task-dra.html)
- Releasing files: [`file-release.html`](https://docs.aws.amazon.com/fsx/latest/LustreGuide/file-release.html)
- POSIX metadata support for data repositories: [`posix-metadata-support.html`](https://docs.aws.amazon.com/fsx/latest/LustreGuide/posix-metadata-support.html)
- Walkthrough: attaching POSIX permissions when uploading: [`attach-s3-posix-permissions.html`](https://docs.aws.amazon.com/fsx/latest/LustreGuide/attach-s3-posix-permissions.html)
- Deployment types and storage classes: [`using-fsx-lustre.html`](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html)
- Service quotas: [`limits.html`](https://docs.aws.amazon.com/fsx/latest/LustreGuide/limits.html)
- Working with SSE-encrypted S3 buckets: [`s3-server-side-encryption-support.html`](https://docs.aws.amazon.com/fsx/latest/LustreGuide/s3-server-side-encryption-support.html)
- Data repository event logs: [`data-repo-event-logs.html`](https://docs.aws.amazon.com/fsx/latest/LustreGuide/data-repo-event-logs.html)

### AWS API / CLI

- `CreateDataRepositoryAssociation`: [FSx API Reference](https://docs.aws.amazon.com/fsx/latest/APIReference/API_CreateDataRepositoryAssociation.html)
- `UpdateDataRepositoryAssociation`: [FSx API Reference](https://docs.aws.amazon.com/fsx/latest/APIReference/API_UpdateDataRepositoryAssociation.html)
- `CreateDataRepositoryTask`: [FSx API Reference](https://docs.aws.amazon.com/fsx/latest/APIReference/API_CreateDataRepositoryTask.html)
- `aws fsx create-data-repository-association`: [AWS CLI reference](https://docs.aws.amazon.com/cli/latest/reference/fsx/create-data-repository-association.html)
- `aws fsx create-data-repository-task`: [AWS CLI reference](https://docs.aws.amazon.com/cli/latest/reference/fsx/create-data-repository-task.html)

### Kubernetes / EKS

- FSx for Lustre CSI driver: [github.com/kubernetes-sigs/aws-fsx-csi-driver](https://github.com/kubernetes-sigs/aws-fsx-csi-driver)

### Lustre HSM

- Lustre HSM (community docs, general background on `hsm_state`, `hsm_action`, `hsm_restore`, `hsm_release`, `hsm_archive`): [wiki.lustre.org/HSM](https://wiki.lustre.org/HSM)
- `stat(2)` reference for the file-mode integer: [`man7.org/linux/man-pages/man2/lstat.2.html`](https://man7.org/linux/man-pages/man2/lstat.2.html)
