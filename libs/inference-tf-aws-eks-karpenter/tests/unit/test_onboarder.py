"""Unit tests for the onboarder logic.

Imports engine/onboarder.py directly (no CodeBuild, no cluster) and exercises:
  - the pure core: field-path parsing/get/set, image-ref splitting, weight-name derivation;
  - Path A (Helm chart -> overrides.yaml) against tests/unit/charts/mock-chart(+ -broken);
  - Path B (KRO graph -> graph-air-gapped.yaml) against tests/unit/charts/mock-graph.

The side-effecting Runner is FAKED (fixed digest, no-op copies) so the parse -> vendor ->
emit -> backstop logic runs deterministically; helm is real for Path A's render backstop.
The live variant (real skopeo/s5cmd/CodeBuild) is the gated e2e test.
"""

import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml
from botocore.exceptions import ClientError

from inference_tf_aws_eks_karpenter.template import TEMPLATE_PATH

CHARTS = Path(__file__).resolve().parent / "charts"
FAKE_DIGEST = "sha256:deadbeefcafe0000000000000000000000000000000000000000000000000000"
ECR = "123456789012.dkr.ecr.us-west-2.amazonaws.com"
MODELS = "s3://inference-abc-store/models"

_HELM = shutil.which("helm")


def _load_module() -> Any:
    """Import engine/onboarder.py by path (it ships as template payload, not a package)."""
    path = TEMPLATE_PATH / "engine" / "onboarder.py"
    spec = importlib.util.spec_from_file_location("onboarder", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass can resolve the module namespace during class body.
    sys.modules["onboarder"] = mod
    spec.loader.exec_module(mod)
    return mod


co = _load_module()


class FakeRunner(co.Runner):
    """Resolves a fixed digest, no-ops every copy; delegates helm_template to real helm."""

    def __init__(self) -> None:
        super().__init__(dry_run=True)
        self.copied: list[tuple[str, str]] = []
        self.ingested: list[tuple[str, str]] = []

    def resolve_digest(self, src_ref: str) -> str:
        return FAKE_DIGEST

    def copy_image(self, src_ref: str, dst_digest_ref: str, dst_tag_ref: str) -> None:
        self.copied.append((src_ref, dst_digest_ref))

    def ingest_weights(self, source: str, dst_uri: str, name: str) -> None:
        self.ingested.append((source, dst_uri))


def _onboarder(runner: co.Runner) -> Any:
    return co.Onboarder(ecr_registry=ECR, workload_prefix="workload", models_s3_uri=MODELS, runner=runner)


class TestPureCore(unittest.TestCase):
    def test_parse_path_handles_dotted_and_bracket_indices(self) -> None:
        self.assertEqual(
            co.parse_path("resources[0].template.spec.containers[1].image"),
            ["resources", 0, "template", "spec", "containers", 1, "image"],
        )
        self.assertEqual(co.parse_path("a"), ["a"])

    def test_parse_path_rejects_malformed(self) -> None:
        for bad in ["", "a..b", "a[x]"]:
            with self.assertRaises(ValueError):
                co.parse_path(bad)

    def test_get_and_set_path_roundtrip(self) -> None:
        obj = {"resources": [{"template": {"containers": [{"image": "up/stream:1"}]}}]}
        tokens = co.parse_path("resources[0].template.containers[0].image")
        self.assertEqual(co.get_path(obj, tokens), "up/stream:1")
        co.set_path(obj, tokens, "ecr/repo@sha256:x")
        self.assertEqual(obj["resources"][0]["template"]["containers"][0]["image"], "ecr/repo@sha256:x")

    def test_split_image_ref(self) -> None:
        self.assertEqual(
            co.split_image_ref("public.ecr.aws/docker/library/busybox:1.36"),
            ("public.ecr.aws", "docker/library/busybox", "1.36"),
        )
        self.assertEqual(co.split_image_ref("vllm/vllm-openai:v0.6.6"), ("", "vllm/vllm-openai", "v0.6.6"))
        # digest stripped; host detected by the dotted first segment
        self.assertEqual(co.split_image_ref("quay.io/x/y@sha256:abc"), ("quay.io", "x/y", None))

    def test_weight_name_from_source(self) -> None:
        self.assertEqual(co.weight_name_from_source("hf://google/Gemma-2-9b"), "gemma-2-9b")
        self.assertEqual(co.weight_name_from_source("hf://google/Gemma-2-9b@abc123"), "gemma-2-9b")
        self.assertEqual(co.weight_name_from_source("s3://b/pre/My-Model/"), "my-model")

    def test_split_weight_entry(self) -> None:
        self.assertEqual(
            co.split_weight_entry("spec.a[0].env[0].value=qwen2.5-7b"), ("spec.a[0].env[0].value", "qwen2.5-7b")
        )
        self.assertEqual(co.split_weight_entry("spec.a[0].source"), ("spec.a[0].source", None))

    def test_detect_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "Chart.yaml").write_text("name: x\n")
            self.assertEqual(co.detect_mode(d), "chart")
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "graph.yaml").write_text("metadata:\n  name: x\n")
            self.assertEqual(co.detect_mode(d), "graph")

    def test_split_s3_uri(self) -> None:
        self.assertEqual(co.split_s3_uri("s3://bucket/pre/fix/"), ("bucket", "pre/fix"))
        self.assertEqual(co.split_s3_uri("s3://bucket"), ("bucket", ""))
        with self.assertRaises(ValueError):
            co.split_s3_uri("s3://")

    def test_part_ranges(self) -> None:
        part = co._S3_PART_BYTES
        # under one part -> single inclusive range covering the whole object
        self.assertEqual(co.part_ranges(100), [(0, 99)])
        # exact multiple -> no empty trailing range
        self.assertEqual(co.part_ranges(2 * part), [(0, part - 1), (part, 2 * part - 1)])
        # remainder -> last range is short and inclusive-clamped to size-1
        self.assertEqual(co.part_ranges(part + 10), [(0, part - 1), (part, part + 9)])
        self.assertEqual(co.part_ranges(0), [])
        with tempfile.TemporaryDirectory() as td, self.assertRaises(SystemExit):
            co.detect_mode(Path(td))

    def test_ecr_tag_args(self) -> None:
        # JSON map -> `--tags Key=..,Value=..` shorthand items (one per tag).
        with patch.dict("os.environ", {"RESOURCE_TAGS_JSON": '{"DeploymentId": "abc123", "Source": "jupyter-deploy"}'}):
            args = co.Runner._ecr_tag_args()
        self.assertEqual(args[0], "--tags")
        self.assertIn("Key=DeploymentId,Value=abc123", args)
        self.assertIn("Key=Source,Value=jupyter-deploy", args)
        # Unset or empty -> no --tags flag at all (create-repository stays valid).
        with patch.dict("os.environ", {"RESOURCE_TAGS_JSON": "{}"}):
            self.assertEqual(co.Runner._ecr_tag_args(), [])
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(co.Runner._ecr_tag_args(), [])

    @patch("subprocess.run")
    def test_hugging_face_revision_is_pinned(self, run: Any) -> None:
        runner = co.Runner()

        runner.ingest_weights("hf://PaddlePaddle/PP-DocLayoutV3_onnx@abc123", f"{MODELS}/layout", "layout")

        self.assertEqual(
            run.call_args_list[0].args[0],
            [
                "hf",
                "download",
                "PaddlePaddle/PP-DocLayoutV3_onnx",
                "--local-dir",
                "/tmp/hf/layout",
                "--revision",
                "abc123",
            ],
        )

    def test_hugging_face_revision_cannot_be_empty(self) -> None:
        runner = co.Runner()

        with self.assertRaises(SystemExit):
            runner.ingest_weights("hf://PaddlePaddle/PP-DocLayoutV3_onnx@", f"{MODELS}/layout", "layout")


@unittest.skipIf(_HELM is None, "helm not on PATH — required for the Path-A onboard backstop")
class TestPathAChart(unittest.TestCase):
    def _stage(self, tmp: Path, name: str) -> Path:
        chart = tmp / name
        shutil.copytree(CHARTS / name, chart)
        return chart

    def test_conforming_chart_rewrites_to_ecr_digest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            chart = self._stage(tmp, "mock-chart")
            result = co.onboard(chart, tmp, _onboarder(FakeRunner()))

            self.assertEqual(result.output_basename, "overrides.yaml")
            self.assertEqual(result.name, "mock-chart")
            ovr = yaml.safe_load(result.output_file.read_text())
            self.assertEqual(ovr["images"]["server"]["registry"], ECR)
            self.assertEqual(ovr["images"]["server"]["repository"], "workload/docker/library/busybox")
            self.assertEqual(ovr["images"]["server"]["tag"], f"@{FAKE_DIGEST}")

    def test_weights_block_rewritten_to_our_s3(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            chart = self._stage(tmp, "mock-chart")
            # Source's last segment (v1.0.0) deliberately DIFFERS from the declared name
            # (mock-tiny): the dst subdir MUST be the declared name — the workload reads
            # /models/<name>, so deriving from the source's last segment would 404 it.
            with (chart / "values.yaml").open("a") as f:
                f.write('\nweights:\n  model:\n    source: "s3://some-src/artifacts/v1.0.0"\n    name: mock-tiny\n')
            result = co.onboard(chart, tmp, _onboarder(FakeRunner()))
            ovr = yaml.safe_load(result.output_file.read_text())
            self.assertEqual(ovr["weights"]["model"]["source"], f"{MODELS}/mock-tiny")

    def test_broken_chart_fails_the_backstop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            chart = self._stage(tmp, "mock-chart-broken")
            with self.assertRaises(SystemExit) as cm:
                co.onboard(chart, tmp, _onboarder(FakeRunner()))
            self.assertIn("BACKSTOP FAILED", str(cm.exception))


class TestPathBGraph(unittest.TestCase):
    def _stage(self, tmp: Path, name: str) -> Path:
        d = tmp / name
        shutil.copytree(CHARTS / name, d)
        return d

    def test_graph_rewrites_paths_and_leaves_original_pristine(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            graph = self._stage(tmp, "mock-graph")
            original = (graph / "graph.yaml").read_text()

            result = co.onboard(graph, tmp, _onboarder(FakeRunner()))
            self.assertEqual(result.output_basename, "graph-air-gapped.yaml")
            self.assertEqual(result.name, "servable-mock")

            # original graph.yaml untouched
            self.assertEqual((graph / "graph.yaml").read_text(), original)

            emitted = yaml.safe_load(result.output_file.read_text())
            image_paths = yaml.safe_load((graph / "values.yaml").read_text())["images"]
            weight_paths = yaml.safe_load((graph / "values.yaml").read_text())["weights"]
            for p in image_paths:
                val = co.get_path(emitted, co.parse_path(p))
                self.assertTrue(val.startswith(f"{ECR}/workload/") and f"@{FAKE_DIGEST}" in val, val)
            for entry in weight_paths:
                path, name = co.split_weight_entry(entry)
                val = co.get_path(emitted, co.parse_path(path))
                # the =name in the sidecar (mock-tiny) drives the models/<name> subdir,
                # NOT the source's last segment — so the workload read-path matches.
                self.assertEqual(val, f"{MODELS}/{name}")

    def test_graph_without_image_paths_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            graph = self._stage(tmp, "mock-graph")
            (graph / "values.yaml").write_text("images: []\n")
            with self.assertRaises(SystemExit):
                co.onboard(graph, tmp, _onboarder(FakeRunner()))


class FakeS3Client:
    """In-memory S3 double: serves source objects by byte-range, records dest writes, and
    tracks multipart lifecycle so a test can assert bytes round-trip and no upload is orphaned.

    Models BOTH copy paths: server-side (copy_object / upload_part_copy, the primary) reads
    straight from the source map into dest/parts; byte-streaming (get_object -> put_object /
    upload_part, the fallback) does the same via a read-then-write."""

    def __init__(self, source: dict[tuple[str, str], bytes]) -> None:
        self.source = source  # (bucket, key) -> bytes
        self.dest: dict[tuple[str, str], bytes] = {}
        self._mpu: dict[str, dict[str, Any]] = {}  # upload_id -> {key, parts}
        self.aborted: list[str] = []
        self._next_id = 0

    def get_paginator(self, _op: str) -> Any:
        source = self.source

        class _P:
            def paginate(self, *, Bucket: str, Prefix: str) -> Any:
                contents = [{"Key": k} for (b, k) in source if b == Bucket and k.startswith(Prefix)]
                return [{"Contents": contents}]

        return _P()

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, int]:
        return {"ContentLength": len(self.source[(Bucket, Key)])}

    def get_object(self, *, Bucket: str, Key: str, Range: str | None = None) -> Any:
        data = self.source[(Bucket, Key)]
        if Range:
            start, end = (int(x) for x in Range.removeprefix("bytes=").split("-"))
            data = data[start : end + 1]

        class _B:
            def __init__(self, b: bytes) -> None:
                self._b = b

            def read(self) -> bytes:
                return self._b

        return {"Body": _B(data)}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> None:
        self.dest[(Bucket, Key)] = Body

    def create_multipart_upload(self, *, Bucket: str, Key: str) -> dict[str, str]:
        self._next_id += 1
        uid = f"u{self._next_id}"
        self._mpu[uid] = {"bucket": Bucket, "key": Key, "parts": {}}
        self.peak_open_mpu = max(getattr(self, "peak_open_mpu", 0), len(self._mpu))
        return {"UploadId": uid}

    def upload_part(self, *, Bucket: str, Key: str, PartNumber: int, UploadId: str, Body: bytes) -> dict[str, str]:
        self._mpu[UploadId]["parts"][PartNumber] = Body
        return {"ETag": f"etag-{PartNumber}"}

    def complete_multipart_upload(self, *, Bucket: str, Key: str, UploadId: str, MultipartUpload: dict) -> None:
        mpu = self._mpu.pop(UploadId)
        ordered = sorted(mpu["parts"].items())
        self.dest[(Bucket, Key)] = b"".join(body for _, body in ordered)

    def abort_multipart_upload(self, *, Bucket: str, Key: str, UploadId: str) -> None:
        self.aborted.append(UploadId)
        self._mpu.pop(UploadId, None)

    # --- server-side copy primitives (the primary path) ---

    def copy_object(self, *, Bucket: str, Key: str, CopySource: dict[str, str]) -> dict[str, Any]:
        self.dest[(Bucket, Key)] = self.source[(CopySource["Bucket"], CopySource["Key"])]
        return {"CopyObjectResult": {"ETag": "etag-copy"}}

    def upload_part_copy(
        self, *, Bucket: str, Key: str, PartNumber: int, UploadId: str, CopySource: dict[str, str], CopySourceRange: str
    ) -> dict[str, Any]:
        data = self.source[(CopySource["Bucket"], CopySource["Key"])]
        start, end = (int(x) for x in CopySourceRange.removeprefix("bytes=").split("-"))
        self._mpu[UploadId]["parts"][PartNumber] = data[start : end + 1]
        return {"CopyPartResult": {"ETag": f"etag-{PartNumber}"}}


class TestS3Copy(unittest.TestCase):
    """The weight copy (Runner._copy_s3_prefix) against a faked S3 client — asserts bytes
    round-trip via both the single-shot and multipart paths on the server-side primary, and
    that a copy-refusing source falls back to byte-streaming and still lands byte-exact."""

    def _run(self, source: dict[tuple[str, str], bytes]) -> FakeS3Client:
        fake = FakeS3Client(source)
        runner = co.Runner()
        runner._s3_client = fake  # inject the double; bypass real boto3
        runner._copy_s3_prefix("s3://src/models/m1", "s3://dst/models/m1")
        return fake

    def test_small_object_single_shot(self) -> None:
        fake = self._run({("src", "models/m1/config.json"): b"hello"})
        self.assertEqual(fake.dest[("dst", "models/m1/config.json")], b"hello")
        self.assertEqual(fake.aborted, [])

    def test_large_object_multipart_roundtrips_bytes(self) -> None:
        blob = bytes((i % 256) for i in range(co._S3_PART_BYTES * 2 + 123))  # > 2 parts
        fake = self._run({("src", "models/m1/model.safetensors"): blob})
        self.assertEqual(fake.dest[("dst", "models/m1/model.safetensors")], blob)
        self.assertEqual(fake.aborted, [])

    def test_prefix_relative_keys_preserved(self) -> None:
        fake = self._run(
            {
                ("src", "models/m1/a.bin"): b"a",
                ("src", "models/m1/sub/b.bin"): b"b",
            }
        )
        self.assertEqual(fake.dest[("dst", "models/m1/a.bin")], b"a")
        self.assertEqual(fake.dest[("dst", "models/m1/sub/b.bin")], b"b")

    def test_empty_prefix_fails(self) -> None:
        with self.assertRaises(SystemExit):
            self._run({("src", "other/x"): b"x"})

    def test_open_multipart_uploads_are_bounded(self) -> None:
        """Many multipart objects must NOT all open at once (a crash would orphan them):
        peak concurrently-open MPUs stays within the file-concurrency cap, and every upload
        is finalized (nothing left open)."""
        big = co._S3_PART_BYTES * 2
        n = 40  # > _S3_MAX_CONCURRENT_FILES
        fake = self._run({("src", f"models/m1/shard{i:03}.bin"): bytes(big) for i in range(n)})
        self.assertLessEqual(fake.peak_open_mpu, co._S3_MAX_CONCURRENT_FILES)
        self.assertEqual(len(fake._mpu), 0, "every multipart upload must be completed (none left open)")
        self.assertEqual(len(fake.dest), n)

    def test_multipart_aborts_on_upload_failure(self) -> None:
        blob = bytes(co._S3_PART_BYTES * 2)  # forces multipart
        fake = FakeS3Client({("src", "models/m1/big.bin"): blob})

        def boom(**_kw: Any) -> dict[str, str]:
            raise RuntimeError("part copy failed")

        fake.upload_part_copy = boom  # type: ignore[method-assign]
        runner = co.Runner()
        runner._s3_client = fake
        with self.assertRaises(RuntimeError):
            runner._copy_s3_prefix("s3://src/models/m1", "s3://dst/models/m1")
        self.assertEqual(len(fake.aborted), 1, "a failed multipart must be aborted (no orphan upload)")

    def test_one_files_failure_aborts_only_that_file(self) -> None:
        """With many multipart objects in flight, a failure in ONE must abort only that
        object's upload and still surface the error — the others complete independently."""
        big = co._S3_PART_BYTES * 2
        fake = FakeS3Client(
            {
                ("src", "models/m1/good.bin"): bytes(big),
                ("src", "models/m1/bad.bin"): bytes(big),
                ("src", "models/m1/tiny.txt"): b"ok",  # single-shot, unaffected
            }
        )
        real_upload_part_copy = fake.upload_part_copy

        def selective(*, UploadId: str, **kw: Any) -> dict[str, Any]:
            # Fail only the "bad.bin" upload (its upload_id maps to that key).
            if fake._mpu.get(UploadId, {}).get("key", "").endswith("bad.bin"):
                raise RuntimeError("bad.bin part failed")
            return real_upload_part_copy(UploadId=UploadId, **kw)

        fake.upload_part_copy = selective  # type: ignore[method-assign]
        runner = co.Runner()
        runner._s3_client = fake
        with self.assertRaises(RuntimeError):
            runner._copy_s3_prefix("s3://src/models/m1", "s3://dst/models/m1")

        # the healthy multipart object AND the single-shot object completed...
        self.assertEqual(fake.dest[("dst", "models/m1/good.bin")], bytes(big))
        self.assertEqual(fake.dest[("dst", "models/m1/tiny.txt")], b"ok")
        # ...and exactly the failed object's upload was aborted (never completed / left open).
        self.assertNotIn(("dst", "models/m1/bad.bin"), fake.dest)
        self.assertEqual(len(fake.aborted), 1)

    def test_copy_refused_source_falls_back_to_streaming(self) -> None:
        """A source that grants GetObject but refuses the copy-source read (AccessDenied on
        copy_object/upload_part_copy) must fall back to byte-streaming per object and still
        land byte-exact — on both the single-shot and the multipart path."""
        small = b"tiny-weights"
        big = bytes((i % 256) for i in range(co._S3_PART_BYTES * 2 + 7))  # > 2 parts
        fake = FakeS3Client(
            {
                ("src", "models/m1/config.json"): small,
                ("src", "models/m1/model.safetensors"): big,
            }
        )

        refused = ClientError({"Error": {"Code": "AccessDenied", "Message": "copy source denied"}}, "UploadPartCopy")

        def refuse(**_kw: Any) -> dict[str, Any]:
            raise refused

        fake.copy_object = refuse  # type: ignore[method-assign]
        fake.upload_part_copy = refuse  # type: ignore[method-assign]
        runner = co.Runner()
        runner._s3_client = fake
        runner._copy_s3_prefix("s3://src/models/m1", "s3://dst/models/m1")

        self.assertEqual(fake.dest[("dst", "models/m1/config.json")], small)
        self.assertEqual(fake.dest[("dst", "models/m1/model.safetensors")], big)
        # The multipart object opened a server-side MPU, saw its parts refused, and aborted it
        # (no orphan) before the streaming fallback re-copied via a fresh, completed MPU. The
        # single-shot object never opened an MPU. So exactly one abort — the refused MPU.
        self.assertEqual(len(fake.aborted), 1)


if __name__ == "__main__":
    unittest.main()
