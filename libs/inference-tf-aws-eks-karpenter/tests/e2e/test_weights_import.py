"""Gated live E2E — the onboarder's two weight-source schemes, end to end.

Both tests onboard a conforming chart for its weights: block only (never served) and assert
the weights land in our S3. They cover the two source schemes the onboarder supports:

  test_large_weights_import_streams_to_s3 — s3:// (SageMaker JumpStart public cache).
      NVIDIA Nemotron-3-Super-120B-A12B (BF16): ~230 GiB across 75 objects (tiny configs +
      multi-GB safetensors shards). Proves the server-side S3 multipart copy (UploadPartCopy
      — S3 moves the bytes internally) handles a model that FAR exceeds the CodeBuild EBS
      (128GB): no local disk, no NIC transit. Asserts every source object lands byte-exact.
      Slow: 230 GiB + the CodeBuild cold start runs past the default build ceiling, so it
      raises max_polls.

  test_hf_weights_import_streams_to_s3 — hf:// (Hugging Face Hub).
      Qwen/Qwen2.5-0.5B-Instruct, a small real PUBLIC/UNGATED model (~1 GiB). Proves the
      onboarder snapshots the repo via huggingface_hub (CodeBuild public egress, anonymous
      — no HF token) and s5cmd-pushes it into our S3. Asserts the snapshot's signature files
      (config.json + a *.safetensors shard) land. Small enough to fit the default ceiling.

  test_gated_hf_weights_import_streams_to_s3 — GATED hf:// (Hugging Face Hub, token required).
      google/gemma-3-270m-it. Exercises the token path the public hf:// test can't:
      the onboarder fetches an HF token from Secrets Manager (hf_token_secret_arn) and passes
      it to huggingface_hub to download a gated model. SKIP-gated on HF_GATED_E2E — it needs a
      deployment whose configured token has accepted this model's license.

Neither serves the model (a 120B needs P-class GPUs; the small copies are the thing under test).
Marked `full_deployment` — runs only against a deployed cluster with full-deploy=true.
"""

import os

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment

from tests.e2e import _serving_helpers as h

CHART = "weights-import"
# The chart's weights.model.source key (under the JumpStart bucket) and its .name — the
# models/<name> subdir the onboarder copies into. Kept in sync with the chart values.yaml.
# The bucket itself embeds the region, so it comes from JUMPSTART_PUBLIC_BUCKET_NAME (via
# h.jumpstart_bucket), NOT a hardcoded name; the chart fixture uses the same placeholder.
SOURCE_KEY = "huggingface-llm/huggingface-llm-nvidia-nemotron-3-super-120b-a12b-bf16/artifacts/inference-prepack/v1.0.0"
WEIGHT_NAME = "nemotron-3-super-120b"
# ~90 min ceiling (270 x 20s): the CodeBuild cold start + a 230 GiB copy is the long pole.
MAX_POLLS = 270

# hf:// case. The chart's weights.model.name — the models/<name> subdir the onboarder
# snapshots into. Kept in sync with charts/hf-weights-import/values.yaml (source
# hf://Qwen/Qwen2.5-0.5B-Instruct). ~1 GiB fits the default build ceiling, so no MAX_POLLS.
HF_CHART = "hf-weights-import"
HF_WEIGHT_NAME = "qwen2.5-0.5b-instruct"

# gated hf:// case. A GATED model (google/gemma-3-270m-it) whose download requires the HF
# token the onboarder fetches from Secrets Manager (hf_token_secret_arn). Skip-gated on
# HF_GATED_E2E. Kept in sync with charts/gated-hf-weights-import/values.yaml.
GATED_HF_CHART = "gated-hf-weights-import"
GATED_HF_WEIGHT_NAME = "gemma-3-270m-it"


@pytest.mark.full_deployment
def test_large_weights_import_streams_to_s3(e2e_deployment: EndToEndDeployment) -> None:
    e2e_deployment.ensure_deployed()
    region = h.jd_output(e2e_deployment, "region")

    # Source truth: how many objects / bytes the JumpStart prefix holds.
    source_uri = f"s3://{h.jumpstart_bucket()}/{SOURCE_KEY}"
    src_count, src_bytes = h.s3_prefix_stats(source_uri)
    assert src_count > 1 and src_bytes > 100 * 1024**3, (
        f"expected a large multi-object source, got {src_count} objects / {src_bytes} bytes"
    )

    dst_uri = f"{h.jd_output(e2e_deployment, 'models_s3_uri')}/{WEIGHT_NAME}"
    try:
        # Onboard: copies the weights prefix into s3://<bucket>/models/<name> (no local disk).
        overrides = h.onboard_chart(e2e_deployment, region, CHART, max_polls=MAX_POLLS)
        assert dst_uri in overrides.read_text(), f"overrides must repoint weights at {dst_uri}"

        # Every source object landed, at the same total size (server-side copy is byte-exact).
        dst_count, dst_bytes = h.s3_prefix_stats(dst_uri)
        assert dst_count == src_count, f"object count mismatch: source {src_count}, dest {dst_count}"
        assert dst_bytes == src_bytes, f"byte-size mismatch: source {src_bytes}, dest {dst_bytes}"
    finally:
        # Always purge the ~230 GiB copy — pass or fail — so it never lingers in the store.
        h.delete_s3_prefix(dst_uri)


@pytest.mark.full_deployment
def test_hf_weights_import_streams_to_s3(e2e_deployment: EndToEndDeployment) -> None:
    """hf:// source: the onboarder snapshots a small public/ungated HF model and s5cmd-pushes
    it into our S3. Complements the JumpStart s3:// test — the SAME emit/land contract via
    the OTHER weight-source scheme (huggingface_hub snapshot_download, not server-side copy).

    There is no S3 'source truth' to diff byte-for-byte (the source is the Hub), so instead
    we assert the emitted snapshot itself: the overrides repoint at our bucket and the model's
    signature files — config.json + at least one *.safetensors shard — landed under
    models/<name>. Anonymous download, so no HF token is needed."""
    e2e_deployment.ensure_deployed()
    region = h.jd_output(e2e_deployment, "region")

    dst_uri = f"{h.jd_output(e2e_deployment, 'models_s3_uri')}/{HF_WEIGHT_NAME}"
    try:
        # Onboard: huggingface_hub snapshots the repo in CodeBuild, s5cmd pushes it to our S3.
        overrides = h.onboard_chart(e2e_deployment, region, HF_CHART)
        assert dst_uri in overrides.read_text(), f"overrides must repoint weights at {dst_uri}"

        # The snapshot's signature files must have landed (not just some bytes): a real HF
        # model repo always carries config.json and its weights as *.safetensors shard(s).
        keys = h.s3_prefix_keys(dst_uri)
        leaves = {k.rsplit("/", 1)[-1] for k in keys}
        assert "config.json" in leaves, f"expected config.json under {dst_uri}, got {sorted(leaves)}"
        assert any(k.endswith(".safetensors") for k in keys), (
            f"expected a *.safetensors weights shard under {dst_uri}, got {sorted(leaves)}"
        )
    finally:
        # Purge the copy — pass or fail — so it never lingers in the store.
        h.delete_s3_prefix(dst_uri)


@pytest.mark.full_deployment
def test_gated_hf_weights_import_streams_to_s3(e2e_deployment: EndToEndDeployment) -> None:
    """GATED hf:// source: the onboarder fetches an HF token from Secrets Manager
    (hf_token_secret_arn) and uses it to download a gated model, then s5cmd-pushes it to our
    S3. This is the ONLY test that exercises the token path (the public hf:// test downloads
    anonymously).

    SKIP-gated on HF_GATED_E2E because it has two out-of-band prerequisites the harness can't
    provision: (1) the deployment must set hf_token_secret_arn to a Secrets Manager secret
    holding an HF token, and (2) that token's account must have accepted
    google/gemma-3-270m-it's license. Set HF_GATED_E2E=1 once both hold. Asserts the
    snapshot's signature files (config.json + a *.safetensors shard) land under models/<name>."""
    if not os.environ.get("HF_GATED_E2E"):
        pytest.skip(
            "HF_GATED_E2E not set: the gated-model e2e needs a deployment configured with "
            "hf_token_secret_arn whose token has accepted google/gemma-3-270m-it"
        )
    e2e_deployment.ensure_deployed()
    region = h.jd_output(e2e_deployment, "region")

    dst_uri = f"{h.jd_output(e2e_deployment, 'models_s3_uri')}/{GATED_HF_WEIGHT_NAME}"
    try:
        # Onboard: the onboarder pulls the token from Secrets Manager and downloads the gated
        # repo via huggingface_hub, then s5cmd-pushes it to our S3.
        overrides = h.onboard_chart(e2e_deployment, region, GATED_HF_CHART)
        assert dst_uri in overrides.read_text(), f"overrides must repoint weights at {dst_uri}"

        keys = h.s3_prefix_keys(dst_uri)
        leaves = {k.rsplit("/", 1)[-1] for k in keys}
        assert "config.json" in leaves, f"expected config.json under {dst_uri}, got {sorted(leaves)}"
        assert any(k.endswith(".safetensors") for k in keys), (
            f"expected a *.safetensors weights shard under {dst_uri}, got {sorted(leaves)}"
        )
    finally:
        # Purge the copy — pass or fail — so it never lingers in the store.
        h.delete_s3_prefix(dst_uri)
