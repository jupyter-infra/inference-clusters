#!/usr/bin/env python3
"""onboarder: rehost a consumer artifact's images/weights into our ECR/S3.

This cluster runs on an endpoints-only VPC — nodes reach only this account's regional
ECR and S3, no public egress. So a workload's images and weights must be rehosted
before they can run here. This job does that rehost and emits ONE air-gapped artifact
the deployer applies.

TWO input formats are supported and auto-detected from the unpacked artifact dir:

  Path A — Helm chart (Chart.yaml present):
      values.yaml carries an `images:` block (mandated) + optional `weights:` block,
      each a structured object. We digest-vendor every image to <ecr>/workload/<repo>
      and (optionally) ingest weights to s3://<bucket>/models/<name>, then emit
      `overrides.yaml` (same shape, pointing at our ECR/S3). Backstop: `helm template`
      with the overrides, asserting every rendered image resolves to our ECR.

  Path B — KRO graph, no Helm (graph.yaml present):
      graph.yaml is a valid KRO ResourceGraphDefinition with LITERAL upstream refs
      baked in (so it also works on a non-air-gapped cluster). A sidecar values.yaml
      lists the field-paths to rehost:
          images:  [ "resources[0].template.spec...containers[0].image", ... ]
          weights: [ "resources[0].template.spec...containers[0].args[0]", ... ]
          builds:  [ {path, context, name, tag}, ... ]   # images with NO upstream to import
      Any list may be empty — a storage-only block (PV+PVC, no containers/weights) has
      nothing to rehost and its graph.yaml is already air-gapped-clean, so it just gets
      passed through as the emitted air-gapped copy so the deployer's single apply path
      still works. We read the literal ref at each `images:` path, vendor it (skopeo copy),
      and — for each `builds:` entry — BUILD the source dir at `context` into our ECR via
      the image-build primitive (there is no upstream to skopeo-copy). Both then write a
      REWRITTEN COPY —
      `graph-air-gapped.yaml` — with our ECR/S3 refs. graph.yaml is left pristine.
      Backstop: field-level — every listed image-path in the emitted graph resolves to
      our ECR `@sha256:`, every weight-path to our S3 (cluster-independent).

This module is the single source of truth for the onboard logic. It is base64-embedded
into the CodeBuild buildspec (engine/onboarder.tf) and also imported directly by the
unit tests (tests/unit/test_onboarder.py) — the pure core (parse/rewrite/emit)
is exercised without CodeBuild, with the side-effecting Runner faked. Deps are stdlib +
pyyaml + boto3 + huggingface_hub (boto3 for the server-side S3 weight copy, with a
byte-streaming fallback; huggingface_hub for hf:// snapshot downloads) — the CodeBuild
job gets them from its image plus the buildspec pip install, and the tests get them
from the package dependencies; the module stays a single embeddable file.

Env (all required unless noted):
  CHART_DIR         local path to the unpacked artifact (chart or graph dir)
  ECR_REGISTRY      <acct>.dkr.ecr.<region>.amazonaws.com
  WORKLOAD_PREFIX   ECR repo prefix for vendored workload images (cluster-scoped, e.g.
                    "<cluster>/workload"; default "workload")
  MODELS_S3_URI     s3://<bucket>/models  (weights land under here as <name>/)
  OUT_DIR           where the emitted artifact + result manifest are written
                    (default: $CHART_DIR/..)
  IMAGE_BUILD_PROJECT       CodeBuild project that BUILDS a source dir into a workload/* ECR
                    image (the image-build primitive). Required ONLY when a graph values.yaml
                    declares a `builds:` block (an image with no published upstream to import);
                    unused otherwise.
  IMAGE_BUILD_INPUT_S3_URI  s3://<bucket>/image-build/in — where the build-context tarball is
                    uploaded before the build is triggered. Required with a `builds:` block.
  RESOURCE_TAGS_JSON  JSON map of tags applied to any workload/* ECR repo the job creates
                    (attribution + DeploymentId reaping); optional, default no tags
  SKOPEO_EXTRA      extra skopeo args (tests set --dest-tls-verify=false etc.); optional
  DRY_RUN_COPY      "true" => skip the actual skopeo/s5cmd/ecr writes, still resolve
                    digests via `skopeo inspect` and emit the artifact; optional
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import yaml
from botocore.config import Config
from botocore.exceptions import ClientError
from huggingface_hub import snapshot_download

_MB = 1024 * 1024
# S3 weight-copy tuning. The PRIMARY path is server-side copy (UploadPartCopy/CopyObject):
# S3 moves the bytes internally, so no local disk, no NIC transit, and no RAM per part.
# The byte-streaming FALLBACK (source->memory->dest, for sources that refuse copy) is what
# the memory budget below bounds.
_S3_PART_BYTES = 64 * _MB  # multipart part size + the single-shot threshold (64MiB: S3's
#                            recommended part size; keeps parts < the 10k cap even at 100s of GB)
# Cap total in-flight bytes for the STREAMING FALLBACK; part workers = budget / part = 128
# concurrent parts. 8GB sits well under the LARGE onboarder's 16 GiB (see onboarder.tf) with
# headroom for the OS + skopeo; a fixed cap (not adaptive) keeps this predictable. Server-side
# copy holds no bytes in memory, so for the primary path this is just a concurrency number.
_S3_MEM_BUDGET_BYTES = 8 * 1024 * _MB
# Concurrent part transfers = the shared part-pool size. The boto3 client's connection pool
# MUST be >= this (below), or workers block waiting for a connection and throughput collapses
# to the default 10-way — the single most important S3 throughput knob here.
_S3_MAX_WORKERS = max(1, _S3_MEM_BUDGET_BYTES // _S3_PART_BYTES)
# Cap simultaneously-OPEN multipart uploads (one per in-flight object). Without this a
# 200-file model would open 200 MPUs at once — all orphaned as incomplete uploads if the
# job is killed. 16 in-flight files still saturate the part pool for large shards.
_S3_MAX_CONCURRENT_FILES = 16
# Server-side copy raises one of these when the SOURCE refuses a copy-source read (some
# cross-account buckets grant GetObject but not the copy). We fall back to byte-streaming
# for that one object; JumpStart/public + same-account sources never hit this.
_S3_COPY_REFUSED_CODES = frozenset({"AccessDenied", "InvalidRequest", "NotImplemented"})


def log(msg: str) -> None:
    """Emit a prefixed, flushed line so progress interleaves correctly in CodeBuild logs."""
    print(f"[onboard] {msg}", flush=True)


# =========================================================================
# Pure core — path walking, ref parsing, name derivation (no I/O)
# =========================================================================

# A well-formed path: a key (any non-dot/bracket chars) optionally followed by [N]
# indices, then zero or more `.key[N]...` segments. Rejects empty segments (a..b),
# non-integer indices (a[x]), leading/trailing dots.
_PATH_RE = re.compile(r"[^.\[\]]+(?:\[\d+\])*(?:\.[^.\[\]]+(?:\[\d+\])*)*")
_TOKEN = re.compile(r"[^.\[\]]+|\[\d+\]")


def parse_path(path: str) -> list[str | int]:
    """Parse a field-path, return  list of tokens: dict keys (str) and list indices (int).

    Field-path may be of the form `resources[0].template.spec.containers[0].image`.
    Bracketed integers become list indices; dotted segments become dict keys.
    """
    path = path.strip()
    if not _PATH_RE.fullmatch(path):
        raise ValueError(f"malformed field-path: {path!r}")
    tokens: list[str | int] = []
    for m in _TOKEN.finditer(path):
        seg = m.group(0)
        tokens.append(int(seg[1:-1]) if seg.startswith("[") else seg)
    return tokens


def get_path(obj: Any, tokens: list[str | int]) -> Any:
    """Read the single value at a parsed field-path.

    Raises:
        KeyError/IndexError if absent
    """
    cur = obj
    for t in tokens:
        cur = cur[t]
    return cur


def set_path(obj: Any, tokens: list[str | int], value: Any) -> None:
    """Overwrite the value at a parsed field-path in place (the Path-B ref-rewrite step)."""
    cur = obj
    for t in tokens[:-1]:
        cur = cur[t]
    cur[tokens[-1]] = value


def split_image_ref(ref: str) -> tuple[str, str, str | None]:
    """Split a full image ref into (registry, repository, tag).

    A leading path segment is treated as a registry host only if it looks like one
    (contains '.' or ':', or is 'localhost') — the Docker naming rule. Otherwise the
    ref is a Docker Hub short name and registry is "". Any `@digest` is stripped (we
    re-resolve the digest ourselves), tag is the `:tag` after the final path segment.
    """
    name = ref.split("@", 1)[0]
    tag: str | None = None
    slash = name.rfind("/")
    colon = name.rfind(":")
    if colon > slash:
        tag = name[colon + 1 :]
        name = name[:colon]
    first, sep, rest = name.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        return first, rest, tag
    return "", name, tag


def weight_name_from_source(source: str) -> str:
    """Derive the models/<name> leaf for a weight source: its last path segment.

    hf://google/gemma-2-9b -> gemma-2-9b ; s3://b/prefix/my-model/ -> my-model.
    Lowercased; a trailing slash is ignored. Graph/chart authors should point a weight
    source at a well-named leaf (the name becomes the S3 subdir the workload reads).
    """
    body = source.split("://", 1)[-1].partition("@")[0].rstrip("/")
    leaf = body.rsplit("/", 1)[-1]
    if not leaf:
        raise ValueError(f"cannot derive a weight name from source {source!r}")
    return leaf.lower()


def split_weight_entry(entry: str) -> tuple[str, str | None]:
    """Split a Path-B weights sidecar entry `field-path[=name]` into (path, name).

    The optional `=name` is the models/<name> subdir the workload reads from — it MUST
    match the graph's read path (e.g. --model=/models/<name>), which onboard does NOT
    rewrite. Omit it only when the source's last path segment is already that name.
    """
    path, sep, name = entry.partition("=")
    return path.strip(), (name.strip() if sep else None)


def split_s3_uri(uri: str) -> tuple[str, str]:
    """Split s3://bucket/key/prefix into (bucket, key-prefix); trailing slash stripped."""
    rest = uri[len("s3://") :] if uri.startswith("s3://") else uri
    bucket, _, key = rest.partition("/")
    if not bucket:
        raise ValueError(f"malformed s3 uri: {uri!r}")
    return bucket, key.rstrip("/")


def part_ranges(size: int, part: int = _S3_PART_BYTES) -> list[tuple[int, int]]:
    """Split a size into inclusive (start, end) byte ranges of at most `part` bytes each.

    Empty for a zero-byte object (handled by the caller as a single empty put).
    """
    return [(s, min(s + part, size) - 1) for s in range(0, size, part)]


def detect_mode(art_dir: Path) -> str:
    """Auto-detect the artifact format: 'chart' (Chart.yaml) or 'graph' (graph.yaml)."""
    if (art_dir / "Chart.yaml").is_file():
        return "chart"
    if (art_dir / "graph.yaml").is_file():
        return "graph"
    raise SystemExit(f"[onboard] ERROR: {art_dir} has neither Chart.yaml (chart) nor graph.yaml (graph)")


@dataclass
class Result:
    """What the buildspec needs to publish the emitted artifact."""

    name: str  # chart name / graph metadata.name -> rehost/out/<name>/
    output_file: Path  # emitted artifact on local disk
    output_basename: str  # overrides.yaml | graph-air-gapped.yaml


# =========================================================================
# Runner — the ONLY side-effecting seam (skopeo / s5cmd / aws / helm)
# =========================================================================


class Runner:
    """Wraps every external command. Tests subclass this to fake the copies while
    keeping digest resolution deterministic and the helm backstop real."""

    def __init__(self, *, dry_run: bool = False, skopeo_extra: str = "") -> None:
        """dry_run skips the mutating copies (digest resolution still runs); skopeo_extra
        passes extra skopeo flags (e.g. --dest-tls-verify=false) as a space-joined string."""
        self.dry_run = dry_run
        self.skopeo_extra = [a for a in skopeo_extra.split() if a]
        self._s3_client: Any = None
        self._codebuild_client: Any = None

    @property
    def codebuild_client(self) -> Any:
        """boto3 CodeBuild client, built lazily (like s3_client) so dry-run and the
        pure-core unit tests never construct one (and never need AWS creds)."""
        if self._codebuild_client is None:
            region = os.environ["AWS_DEFAULT_REGION"]
            self._codebuild_client = boto3.client("codebuild", region_name=region)
        return self._codebuild_client

    @property
    def s3_client(self) -> Any:
        """boto3 S3 client, built lazily on first real ingest so dry-run and the pure-core
        unit tests never construct one (and never need AWS creds).

        max_pool_connections is sized to the part-pool: with the default 10, our workers
        would starve on connections and throughput collapses to ~10-way. Longer read/connect
        timeouts (120s) tolerate a slow 64MiB streaming-fallback part transfer without a
        spurious failure (the server-side copy path issues no long body transfers)."""
        if self._s3_client is None:
            region = os.environ["AWS_DEFAULT_REGION"]
            self._s3_client = boto3.client(
                "s3",
                region_name=region,
                config=Config(
                    max_pool_connections=_S3_MAX_WORKERS,
                    read_timeout=120,
                    connect_timeout=120,
                    retries={"max_attempts": 5, "mode": "adaptive"},
                ),
            )
        return self._s3_client

    def resolve_digest(self, src_ref: str) -> str:
        """Resolve the immutable digest from the source manifest (read-only; runs even
        under dry-run so overrides are still emitted). Returns 'sha256:...'."""
        out = subprocess.run(
            ["skopeo", "inspect", "--format", "{{.Digest}}", f"docker://{src_ref}"],
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout.strip()

    def ensure_repo(self, repo: str) -> None:
        """Pre-create the ECR repo skopeo will push to (skopeo won't auto-create).

        Describe first; create ONLY when it genuinely doesn't exist. This avoids
        swallowing real errors (throttling, access-denied) the way an unconditional
        create + check=False would — only RepositoryNotFoundException means "create it".

        A created repo is tagged with the deployment's resource tags (RESOURCE_TAGS_JSON) so
        it is attributable and reapable by DeploymentId like every other cluster resource —
        these workload/* repos are created imperatively (not in TF state), so the tags are
        the only handle a cleanup/offboard has on them.
        """
        if self.dry_run:
            return
        region = os.environ["AWS_DEFAULT_REGION"]
        describe = subprocess.run(
            ["aws", "ecr", "describe-repositories", "--repository-names", repo, "--region", region],
            capture_output=True,
            text=True,
        )
        if describe.returncode == 0:
            return  # already exists
        if "RepositoryNotFoundException" not in describe.stderr:
            # Any other failure (throttle, AccessDenied, ...) is real — surface it.
            raise subprocess.CalledProcessError(describe.returncode, describe.args, describe.stdout, describe.stderr)
        create = subprocess.run(
            ["aws", "ecr", "create-repository", "--repository-name", repo, "--region", region, *self._ecr_tag_args()],
            capture_output=True,
            text=True,
        )
        # Tolerate only the describe->create race (a concurrent build won); anything else is real.
        if create.returncode != 0 and "RepositoryAlreadyExistsException" not in create.stderr:
            raise subprocess.CalledProcessError(create.returncode, create.args, create.stdout, create.stderr)

    @staticmethod
    def _ecr_tag_args() -> list[str]:
        """Build `--tags Key=..,Value=..` args from RESOURCE_TAGS_JSON, or [] if unset/empty.

        The env var is the JSON-encoded deployment tag map (see onboarder.tf); ECR's
        create-repository --tags takes shorthand `Key=<k>,Value=<v>` items."""
        tags = json.loads(os.environ.get("RESOURCE_TAGS_JSON") or "{}")
        if not tags:
            return []
        return ["--tags", *[f"Key={k},Value={v}" for k, v in tags.items()]]

    def copy_image(self, src_ref: str, dst_digest_ref: str, dst_tag_ref: str) -> None:
        """Copy the full manifest (multi-arch) with digest preservation enforced."""
        if self.dry_run:
            return
        subprocess.run(
            [
                "skopeo",
                "copy",
                "--all",
                "--preserve-digests",
                *self.skopeo_extra,
                f"docker://{src_ref}",
                f"docker://{dst_tag_ref}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def build_image(
        self, project: str, context_dir: Path, input_uri: str, name: str, tag: str, timeout: int = 1800
    ) -> None:
        """Build context_dir into <ecr>/<workload>/<name>:<tag> via the image-build CodeBuild job.

        For an image with NO published upstream to skopeo-copy (built from source, e.g. aiperf):
        tar the context (Dockerfile at its root) to <input_uri>/<name>/source.tgz, start-build the
        image-build project with SOURCE_REF/IMAGE_NAME/IMAGE_TAG, and BLOCK until it SUCCEEDS — the
        caller's digest rewrite needs the push complete. skopeo copy only mirrors EXISTING images,
        so this is the "build one that doesn't exist yet" path. Raises (fails the onboard) on a
        missing Dockerfile, an upload/start failure, or a non-SUCCEEDED build, so a broken build
        never emits a dangling ref."""
        if self.dry_run:
            return
        if not (context_dir / "Dockerfile").is_file():
            raise SystemExit(f"[onboard] ERROR: build context {context_dir} has no Dockerfile at its root")
        # Tar the context CONTENTS (Dockerfile at the tarball root — the image-build buildspec
        # asserts /tmp/src/Dockerfile) and upload it, then trigger + poll the build. All AWS
        # calls go through boto3 (no aws-CLI shell-out): env values pass as structured fields,
        # so a name/tag with shell/CLI-special chars can't be misparsed or injected. The temp
        # tarball is removed with its dir (TemporaryDirectory) whether or not the upload fails.
        src_bucket, src_prefix = split_s3_uri(input_uri)
        key = f"{src_prefix + '/' if src_prefix else ''}{name}/source.tgz"
        with tempfile.TemporaryDirectory(prefix="onboard-build-") as td:
            tgz = Path(td) / "source.tgz"
            with tarfile.open(tgz, "w:gz") as tf:
                tf.add(context_dir, arcname=".")
            self.s3_client.upload_file(str(tgz), src_bucket, key)
        source_ref = f"s3://{src_bucket}/{key}"

        build_id = self.codebuild_client.start_build(
            projectName=project,
            environmentVariablesOverride=[
                {"name": "SOURCE_REF", "value": source_ref, "type": "PLAINTEXT"},
                {"name": "IMAGE_NAME", "value": name, "type": "PLAINTEXT"},
                {"name": "IMAGE_TAG", "value": tag, "type": "PLAINTEXT"},
            ],
        )["build"]["id"]
        log(f"  build: started {project} build {build_id} for {name}:{tag}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(15)
            try:
                builds = self.codebuild_client.batch_get_builds(ids=[build_id])["builds"]
            except ClientError as e:
                # Tolerate a transient throttle/5xx mid-poll — keep polling until the deadline.
                log(f"  build: transient error polling {build_id} ({e}); retrying")
                continue
            status = builds[0]["buildStatus"] if builds else "IN_PROGRESS"
            if status == "SUCCEEDED":
                return
            if status in ("FAILED", "FAULT", "STOPPED", "TIMED_OUT"):
                raise SystemExit(f"[onboard] ERROR: image-build {build_id} ended: {status}")
        raise SystemExit(f"[onboard] ERROR: image-build {build_id} did not finish within {timeout}s")

    def ingest_weights(self, source: str, dst_uri: str, name: str) -> None:
        """Rehost a weight source into our S3.

        s3:// copies object-by-object server-side via UploadPartCopy/CopyObject (see
        _copy_s3_prefix): S3 moves the bytes internally, so no local disk (the 128GB
        CodeBuild EBS can't hold a 100s-of-GB model), no memory, and no NIC transit — the
        ~4 Gbps single-instance ceiling of a read-then-write copy is bypassed entirely.
        Sources that grant GetObject but refuse the copy-source read (some cross-account
        buckets) fall back per-object to byte-streaming. hf:// snapshots to disk through
        the official huggingface_hub client (the hub API is file-based)."""
        if self.dry_run:
            return
        if source.startswith("s3://"):
            self._copy_s3_prefix(source, dst_uri)
        elif source.startswith("hf://"):
            repo_id = source[len("hf://") :].partition("@")[0]
            stage = f"/tmp/hf/{name}"
            snapshot_download(repo_id=repo_id, local_dir=stage)
            subprocess.run(["s5cmd", "cp", f"{stage}/", f"{dst_uri}/"], check=True)
        else:
            raise SystemExit(f"[onboard] ERROR: unsupported weight source {source!r} (want hf:// or s3://)")

    def _copy_s3_prefix(self, source: str, dst_uri: str) -> None:
        """Copy every object under the source s3:// prefix to the dest prefix, in parallel.

        Two nested pools bound the two resources independently (modeled on JumpStart's
        S3AsyncMultiPartUpload, which parallelizes one object's chunks; the outer pool
        extends that across objects):
          - outer file-pool (<= _S3_MAX_CONCURRENT_FILES): caps simultaneously-OPEN multipart
            uploads, so a 200-file model never opens 200 MPUs that a crash would orphan;
          - inner part-pool (SHARED, workers = _S3_MAX_WORKERS): caps concurrent part copies.
            The primary UploadPartCopy path holds no bytes; the streaming fallback keeps peak
            memory ~= workers x part size (source->memory->dest, no disk).
        A big shard gets many part-workers; many shards keep the file window full. One S3
        client (the onboard role has GetObject on the source buckets + read/write on dest)."""
        src_bucket, src_prefix = split_s3_uri(source)
        dst_bucket, dst_prefix = split_s3_uri(dst_uri)

        keys = self._list_s3_keys(src_bucket, src_prefix)
        if not keys:
            raise SystemExit(f"[onboard] ERROR: no objects under {source!r}")

        part_workers = _S3_MAX_WORKERS
        file_workers = min(len(keys), _S3_MAX_CONCURRENT_FILES)
        with (
            ThreadPoolExecutor(max_workers=part_workers) as part_pool,
            ThreadPoolExecutor(max_workers=file_workers) as file_pool,
        ):
            file_futs = []
            for key in keys:
                rel = key[len(src_prefix) :].lstrip("/") if src_prefix else key
                dst_key = f"{dst_prefix}/{rel}" if dst_prefix else rel
                file_futs.append(file_pool.submit(self._copy_object, part_pool, src_bucket, key, dst_bucket, dst_key))
            # Surface the first object failure (each object already finalized/aborted itself).
            for f in file_futs:
                f.result()

    def _copy_object(self, part_pool: Any, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str) -> None:
        """Copy ONE object server-side, falling back to byte-streaming if the source refuses.

        The PRIMARY path is server-side copy (UploadPartCopy/CopyObject) — S3 moves the bytes,
        so nothing transits this instance. If the SOURCE refuses the copy-source read (a
        cross-account bucket may grant GetObject but not the copy), the whole object is retried
        via byte-streaming. Per-object (not per-part) fallback keeps the two paths from mixing
        within one multipart upload."""
        size = self.s3_client.head_object(Bucket=src_bucket, Key=src_key)["ContentLength"]
        try:
            self._transfer_object(
                part_pool, src_bucket, src_key, dst_bucket, dst_key, size, self._copy_whole, self._copy_part
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") not in _S3_COPY_REFUSED_CODES:
                raise
            log(f"  server-side copy refused for {src_key}; falling back to byte-streaming")
            self._transfer_object(
                part_pool, src_bucket, src_key, dst_bucket, dst_key, size, self._stream_whole, self._stream_part
            )

    def _transfer_object(
        self,
        part_pool: Any,
        src_bucket: str,
        src_key: str,
        dst_bucket: str,
        dst_key: str,
        size: int,
        copy_whole: Any,
        copy_part: Any,
    ) -> None:
        """Move one object with the given primitive pair, opening its MPU only for its lifetime.

        Small objects go single-shot via `copy_whole`. For a multipart object we open the MPU,
        fan its parts through `copy_part` into the SHARED part-pool, drain them ALL (barrier —
        so abort never races a live part), then complete on full success or abort on any
        failure. The MPU is closed before this returns, so at most file-pool-many uploads are
        ever open at once. `copy_whole`/`copy_part` are the server-side or the streaming pair."""
        ranges = part_ranges(size)
        if len(ranges) <= 1:
            copy_whole(src_bucket, src_key, dst_bucket, dst_key)
            return
        upload_id = self.s3_client.create_multipart_upload(Bucket=dst_bucket, Key=dst_key)["UploadId"]
        futs = [
            part_pool.submit(copy_part, src_bucket, src_key, dst_bucket, dst_key, upload_id, i, start, end)
            for i, (start, end) in enumerate(ranges, start=1)
        ]
        wait(futs)  # let every part settle before completing/aborting this upload
        errors = [f.exception() for f in futs if f.exception() is not None]
        if errors:
            with contextlib.suppress(Exception):  # best-effort; surface the ORIGINAL error
                self.s3_client.abort_multipart_upload(Bucket=dst_bucket, Key=dst_key, UploadId=upload_id)
            raise errors[0]
        parts = sorted((f.result() for f in futs), key=lambda p: p["PartNumber"])
        self.s3_client.complete_multipart_upload(
            Bucket=dst_bucket, Key=dst_key, UploadId=upload_id, MultipartUpload={"Parts": parts}
        )

    def _list_s3_keys(self, bucket: str, prefix: str) -> list[str]:
        """Page through the source prefix, returning every object key (skips 0-byte dir markers)."""
        keys: list[str] = []
        for page in self.s3_client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            keys.extend(o["Key"] for o in page.get("Contents", []) if not o["Key"].endswith("/"))
        return keys

    # --- server-side copy (primary): S3 moves the bytes, nothing transits this instance ---

    def _copy_whole(self, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str) -> None:
        """Server-side CopyObject of a sub-part-size object (<=5GiB): one call, no NIC transit."""
        self.s3_client.copy_object(Bucket=dst_bucket, Key=dst_key, CopySource={"Bucket": src_bucket, "Key": src_key})

    def _copy_part(
        self,
        src_bucket: str,
        src_key: str,
        dst_bucket: str,
        dst_key: str,
        upload_id: str,
        part_no: int,
        start: int,
        end: int,
    ) -> dict[str, Any]:
        """Server-side UploadPartCopy of one byte-range; return its {ETag, PartNumber}.

        No NIC transit and no memory buffer — S3 copies the range internally. NOTE the
        response nests the ETag under CopyPartResult (unlike UploadPart, which is top-level)."""
        r = self.s3_client.upload_part_copy(
            Bucket=dst_bucket,
            Key=dst_key,
            UploadId=upload_id,
            PartNumber=part_no,
            CopySource={"Bucket": src_bucket, "Key": src_key},
            CopySourceRange=f"bytes={start}-{end}",
        )
        return {"ETag": r["CopyPartResult"]["ETag"], "PartNumber": part_no}

    # --- byte-streaming (fallback): source->memory->dest, for sources that refuse copy ---

    def _stream_whole(self, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str) -> None:
        """Copy a sub-part-size object in one GetObject->PutObject (no multipart overhead)."""
        body = self.s3_client.get_object(Bucket=src_bucket, Key=src_key)["Body"].read()
        self.s3_client.put_object(Bucket=dst_bucket, Key=dst_key, Body=body)

    def _stream_part(
        self,
        src_bucket: str,
        src_key: str,
        dst_bucket: str,
        dst_key: str,
        upload_id: str,
        part_no: int,
        start: int,
        end: int,
    ) -> dict[str, Any]:
        """Stream one byte-range src->dest as a multipart part; return its {ETag, PartNumber}.

        Returns only the tiny part descriptor (NOT the bytes), so completed futures hold
        negligible memory — the chunk is uploaded and released within this call."""
        chunk = self.s3_client.get_object(Bucket=src_bucket, Key=src_key, Range=f"bytes={start}-{end}")["Body"].read()
        etag = self.s3_client.upload_part(
            Bucket=dst_bucket, Key=dst_key, PartNumber=part_no, UploadId=upload_id, Body=chunk
        )["ETag"]
        return {"ETag": etag, "PartNumber": part_no}

    def helm_template(self, chart_dir: Path, overrides: Path) -> str:
        """Render the chart with the overrides so the backstop can inspect the image refs."""
        out = subprocess.run(
            ["helm", "template", "onboard-check", str(chart_dir), "-f", str(overrides)],
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout


# =========================================================================
# Onboarder — the two adapters over the shared vendor core
# =========================================================================


class Onboarder:
    def __init__(
        self,
        *,
        ecr_registry: str,
        workload_prefix: str,
        models_s3_uri: str,
        runner: Runner,
        image_build_project: str = "",
        image_build_input: str = "",
    ) -> None:
        """Bind the vendor destinations (ECR registry + workload/ repo prefix + models/ S3
        base) and the Runner seam; both onboard paths rehost through these.

        image_build_project / image_build_input are the image-build primitive's coordinates,
        needed ONLY to satisfy a graph's `builds:` block (an image built from source, no
        upstream to import); empty when the consumer declares no builds."""
        self.ecr = ecr_registry.rstrip("/")
        self.prefix = workload_prefix.strip("/")
        self.models = models_s3_uri.rstrip("/")
        self.runner = runner
        self.image_build_project = image_build_project
        self.image_build_input = image_build_input

    # --- shared vendor core ------------------------------------------------

    def _vendor_image(self, src_ref: str, repository: str, tag: str | None) -> tuple[str, str]:
        """Digest-vendor a source image to <ecr>/<prefix>/<repository>@<digest>.

        Returns (dst_repo, digest). `src_ref` is what skopeo pulls; `repository`/`tag`
        are the parsed source coordinates used to name the destination.
        """
        digest = self.runner.resolve_digest(src_ref)
        dst_repo = f"{self.prefix}/{repository}"
        self.runner.ensure_repo(dst_repo)
        dst_digest_ref = f"{self.ecr}/{dst_repo}@{digest}"
        dst_tag_ref = f"{self.ecr}/{dst_repo}:{tag}" if tag else dst_digest_ref
        self.runner.copy_image(src_ref, dst_digest_ref, dst_tag_ref)
        log(f"  image: {src_ref} -> {dst_digest_ref}")
        return dst_repo, digest

    def _vendor_weights(self, source: str, name: str | None = None) -> str:
        """Rehost a weight source into s3://<bucket>/models/<name>; return the dst URI.

        `name` is the S3 subdir the workload reads from — it MUST match the workload's
        read path (e.g. --model=/models/<name>). When omitted it falls back to the
        source's last path segment (Path A always passes an explicit name).
        """
        name = name or weight_name_from_source(source)
        dst_uri = f"{self.models}/{name}"
        self.runner.ingest_weights(source, dst_uri, name)
        log(f"  weights: {source} -> {dst_uri}")
        return dst_uri

    def _build_image(self, context_dir: Path, name: str, tag: str) -> tuple[str, str]:
        """Build a source dir into <ecr>/<prefix>/<name> and return (dst_repo, digest).

        The image-build CodeBuild job builds+pushes <ecr>/<prefix>/<name>:<tag> (public egress
        in CodeBuild, off the air-gapped cluster); we then resolve its digest from OUR registry
        — reachable on the endpoints-only VPC — so the graph is rewritten to an immutable
        @sha256 ref, identical to the vendored-image contract. This is the no-upstream-to-import
        counterpart of _vendor_image. Requires the image-build coordinates (env-supplied)."""
        if not self.image_build_project or not self.image_build_input:
            raise SystemExit(
                "[onboard] ERROR: graph declares a builds: block but IMAGE_BUILD_PROJECT / "
                "IMAGE_BUILD_INPUT_S3_URI are unset — the cluster's image-build primitive is required."
            )
        if not context_dir.is_dir():
            raise SystemExit(f"[onboard] ERROR: build context dir not found: {context_dir}")
        self.runner.build_image(self.image_build_project, context_dir, self.image_build_input, name, tag)
        dst_repo = f"{self.prefix}/{name}"
        digest = self.runner.resolve_digest(f"{self.ecr}/{dst_repo}:{tag}")
        log(f"  build: {context_dir} -> {self.ecr}/{dst_repo}@{digest}")
        return dst_repo, digest

    # --- Path A: Helm chart -> overrides.yaml ------------------------------

    def onboard_chart(self, chart_dir: Path, out_dir: Path) -> Result:
        """Path A: vendor the chart's images:/weights: blocks and emit overrides.yaml,
        backstopped by a helm-template render asserting every image resolves to our ECR."""
        values = yaml.safe_load((chart_dir / "values.yaml").read_text()) or {}
        images = values.get("images") or {}
        if not images:
            raise SystemExit("[onboard] ERROR: chart has no mandated images: block")

        overrides: dict[str, Any] = {"images": {}}
        log(f"rehosting images to {self.ecr}/{self.prefix}/*")
        for key, entry in images.items():
            registry = entry.get("registry") or ""
            repository = entry["repository"]
            tag = str(entry["tag"])
            src_ref = f"{registry}/{repository}:{tag}" if registry else f"docker.io/{repository}:{tag}"
            dst_repo, digest = self._vendor_image(src_ref, repository, tag)
            # Same {registry,repository,tag} shape the chart.image helper renders, but at
            # our ECR with the digest in the tag position ("@sha256:..." -> repo@digest).
            overrides["images"][key] = {"registry": self.ecr, "repository": dst_repo, "tag": f"@{digest}"}

        weights = values.get("weights") or {}
        if weights:
            overrides["weights"] = {}
            log(f"rehosting weights to {self.models}")
            for key, entry in weights.items():
                # The chart's declared `name` is the models/<name> subdir the workload
                # reads (weightsSubPath) — vendor THERE, not to the source's last segment.
                dst_uri = self._vendor_weights(entry["source"], entry["name"])
                # keep {source,name} shape; name stays the chart's declared subdir name
                overrides["weights"][key] = {"source": dst_uri, "name": entry["name"]}

        out_file = out_dir / "overrides.yaml"
        out_file.write_text(
            "# Generated by onboarder. Do not edit by hand.\n" + yaml.safe_dump(overrides, sort_keys=False)
        )

        # Backstop: render WITH overrides; every image: MUST resolve to our ECR.
        log(f"backstop: helm template with overrides, asserting all images resolve to {self.ecr}")
        rendered = self.runner.helm_template(chart_dir, out_file)
        bad = [ln.strip() for ln in rendered.splitlines() if re.match(r"\s*image:", ln) and self.ecr not in ln]
        if bad:
            for ln in bad:
                log(f"  offending: {ln}")
            raise SystemExit(
                "[onboard] BACKSTOP FAILED: image ref(s) do not resolve to our ECR — "
                "every chart image MUST render via the images: block + chart.image helper."
            )

        name = yaml.safe_load((chart_dir / "Chart.yaml").read_text())["name"]
        log(f"SUCCESS: overrides written to {out_file}")
        return Result(name=name, output_file=out_file, output_basename="overrides.yaml")

    # --- Path B: KRO graph -> graph-air-gapped.yaml ------------------------

    def onboard_graph(self, graph_dir: Path, out_dir: Path) -> Result:
        """Path B: rewrite the literal refs at the sidecar's field-paths to our ECR/S3 and
        emit graph-air-gapped.yaml, backstopped field-by-field (cluster-independent)."""
        graph = yaml.safe_load((graph_dir / "graph.yaml").read_text())
        sidecar = yaml.safe_load((graph_dir / "values.yaml").read_text()) or {}
        image_paths = sidecar.get("images") or []
        weight_paths = sidecar.get("weights") or []
        builds = sidecar.get("builds") or []

        # All three lists may be empty — a storage-only block (PV+PVC, no containers,
        # no weights, no builds) has nothing to rehost and just gets its graph.yaml
        # passed through as the emitted air-gapped copy so the deployer's single
        # apply path still works. NOT an error condition.

        if image_paths:
            log(f"rehosting images to {self.ecr}/{self.prefix}/*")
        for path in image_paths:
            tokens = parse_path(path)
            ref = get_path(graph, tokens)
            if not isinstance(ref, str):
                raise SystemExit(f"[onboard] ERROR: image path {path!r} is not a literal string ref: {ref!r}")
            registry, repository, tag = split_image_ref(ref)
            dst_repo, digest = self._vendor_image(ref, repository, tag)
            set_path(graph, tokens, f"{self.ecr}/{dst_repo}@{digest}")

        if builds:
            log(f"building images with no upstream to {self.ecr}/{self.prefix}/*")
            graph_root = graph_dir.resolve()
            for b in builds:
                if not isinstance(b, dict):
                    raise SystemExit(f"[onboard] ERROR: builds entry must be a mapping, got {b!r}")
                missing = [k for k in ("path", "context", "name", "tag") if k not in b]
                if missing:
                    raise SystemExit(f"[onboard] ERROR: builds entry {b!r} missing keys {missing}")
                # Confine the build context under the artifact dir: a `context` of "/" or
                # "../.." would otherwise tar + upload + bake arbitrary files readable by the
                # job into the image. Resolve and require it stays within graph_dir.
                context_dir = (graph_dir / b["context"]).resolve()
                if not context_dir.is_relative_to(graph_root):
                    raise SystemExit(
                        f"[onboard] ERROR: builds context {b['context']!r} escapes the artifact dir ({context_dir})"
                    )
                dst_repo, digest = self._build_image(context_dir, b["name"], str(b["tag"]))
                set_path(graph, parse_path(b["path"]), f"{self.ecr}/{dst_repo}@{digest}")

        if weight_paths:
            log(f"rehosting weights to {self.models}")
            for entry in weight_paths:
                path, name = split_weight_entry(entry)
                tokens = parse_path(path)
                source = get_path(graph, tokens)
                if not isinstance(source, str):
                    raise SystemExit(
                        f"[onboard] ERROR: weight path {path!r} is not a literal string source: {source!r}"
                    )
                set_path(graph, tokens, self._vendor_weights(source, name))

        # Field-level backstop (cluster-independent): each listed path now resolves to us.
        log(f"backstop: every image/build path -> {self.ecr}@sha256:, weights -> {self.models}")
        for path in image_paths + [b["path"] for b in builds]:
            val = get_path(graph, parse_path(path))
            if not (isinstance(val, str) and val.startswith(f"{self.ecr}/") and "@sha256:" in val):
                raise SystemExit(
                    f"[onboard] BACKSTOP FAILED: image path {path!r} did not resolve to our ECR digest: {val!r}"
                )
        for entry in weight_paths:
            path, _ = split_weight_entry(entry)
            val = get_path(graph, parse_path(path))
            if not (isinstance(val, str) and val.startswith(self.models)):
                raise SystemExit(f"[onboard] BACKSTOP FAILED: weight path {path!r} did not resolve to our S3: {val!r}")

        name = graph["metadata"]["name"]
        out_file = out_dir / "graph-air-gapped.yaml"
        out_file.write_text(
            "# Generated by onboarder from graph.yaml. Do not edit by hand.\n" + yaml.safe_dump(graph, sort_keys=False)
        )
        log(f"SUCCESS: air-gapped graph written to {out_file}")
        return Result(name=name, output_file=out_file, output_basename="graph-air-gapped.yaml")


def onboard(art_dir: Path, out_dir: Path, onboarder: Onboarder) -> Result:
    """Dispatch to the chart or graph path based on the auto-detected artifact format."""
    mode = detect_mode(art_dir)
    log(f"detected artifact mode: {mode}")
    if mode == "chart":
        return onboarder.onboard_chart(art_dir, out_dir)
    return onboarder.onboard_graph(art_dir, out_dir)


def main() -> None:
    """CodeBuild entrypoint: build the Runner + Onboarder from env, run onboard, and write
    onboard-result.env for the buildspec's post_build to publish the emitted artifact."""
    try:
        chart_dir = Path(os.environ["CHART_DIR"])
        ecr = os.environ["ECR_REGISTRY"]
        models = os.environ["MODELS_S3_URI"]
    except KeyError as e:
        raise SystemExit(f"[onboard] ERROR: {e.args[0]} required") from e
    out_dir = Path(os.environ.get("OUT_DIR") or chart_dir.parent)
    runner = Runner(
        dry_run=os.environ.get("DRY_RUN_COPY", "false") == "true",
        skopeo_extra=os.environ.get("SKOPEO_EXTRA", ""),
    )
    onboarder = Onboarder(
        ecr_registry=ecr,
        workload_prefix=os.environ.get("WORKLOAD_PREFIX", "workload"),
        models_s3_uri=models,
        runner=runner,
        image_build_project=os.environ.get("IMAGE_BUILD_PROJECT", ""),
        image_build_input=os.environ.get("IMAGE_BUILD_INPUT_S3_URI", ""),
    )
    result = onboard(chart_dir, out_dir, onboarder)
    # Hand the buildspec what to publish, mode-agnostically (post_build sources this).
    (out_dir / "onboard-result.env").write_text(
        f"ONBOARD_NAME={result.name}\n"
        f"ONBOARD_OUTPUT={result.output_file}\n"
        f"ONBOARD_OUTPUT_BASENAME={result.output_basename}\n"
    )


if __name__ == "__main__":
    main()
