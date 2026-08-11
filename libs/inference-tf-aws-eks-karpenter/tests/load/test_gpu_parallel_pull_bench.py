"""Opt-in benchmark for gpu_parallel_image_pull (containerd 2.2 parallel download/unpack).

On one real GPU node it asserts the transfer-plugin keys are active in `containerd config
dump`, then times a cold `crictl pull` with the feature on, flips the node's config off in
place (same instance, concurrency=1), and re-times. Config lands = hard assert; timing is
informational. Excluded from the e2e gate by the `benchmark` marker.
"""

import os
import re

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl

from tests.e2e import _serving_helpers as h
from tests.load import _bench_helpers as b

PROBE = "gpu-parallel-pull-probe"
# Repeat each measured pull to smooth registry/network jitter; report the median.
REPEATS = 3
# Image to pull-time (BENCH_IMAGE env); falls back to onboarding the vLLM workload image.
BENCH_IMAGE_ENV = "BENCH_IMAGE"

TRANSFER_PLUGIN = "io.containerd.transfer.v1.local"
TRANSFER_KEYS = ("max_concurrent_downloads", "concurrent_layer_fetch_buffer", "max_concurrent_unpacks")
# AL2023 nodeadm merges *.toml here into the effective containerd config.
BENCH_DROPIN = "/etc/containerd/config.d/99-bench-parallel-pull.toml"


def _transfer_block(downloads: int, unpacks: int, buffer: int) -> str:
    return (
        f"[plugins.'{TRANSFER_PLUGIN}']\n"
        f"max_concurrent_downloads = {downloads}\n"
        f"concurrent_layer_fetch_buffer = {buffer}\n"
        f"max_concurrent_unpacks = {unpacks}\n"
    )


@pytest.mark.benchmark
def test_gpu_parallel_pull_benchmark(
    e2e_deployment: EndToEndDeployment,
    kubernetes_cluster_login: None,
) -> None:
    e2e_deployment.ensure_deployed()
    image = h.client_image(e2e_deployment)  # ECR pull-through busybox for the debug/probe pods
    bench_ref = _bench_image_ref(e2e_deployment)
    node = ""

    try:
        # 1. One real `gpu` node via a probe pod.
        h.apply_resource("gpu-parallel-pull-probe.yaml", image=image, namespace=h.NAMESPACE)
        assert b.wait_pod_ready(h.NAMESPACE, PROBE, timeout_s=600), "probe pod never Ready (no GPU node?)"
        node = h.assert_on_karpenter_gpu(PROBE)

        # 2. Correctness: the three keys are active under the transfer plugin (booted = ON).
        active = _assert_transfer_keys_active(node, image)
        assert active.get("max_concurrent_downloads") == 20, f"downloads not 20 in active config: {active}"
        assert active.get("max_concurrent_unpacks") == 5, f"unpacks not 5 in active config: {active}"
        cri_transfer = _cri_uses_transfer_service(node, image)

        # 3. Warm the cache once so both measured pulls hit a warm registry (isolates node-side concurrency).
        warm = b.crictl_pull_seconds(node, image, bench_ref, remove_first=False)
        assert warm > 0, f"warmup pull of {bench_ref} failed — is the image reachable from the node?"

        on_times = _timed_pulls(node, image, bench_ref)
        b.write_containerd_dropin(node, image, BENCH_DROPIN, _transfer_block(1, 1, 0))
        # Confirm the flip is live before trusting OFF timings (else a no-op flip reads as OFF==ON).
        off_active = _assert_transfer_keys_active(node, image)
        assert off_active.get("max_concurrent_downloads") == 1, (
            f"OFF flip did not take effect (active downloads={off_active.get('max_concurrent_downloads')}, "
            f"expected 1) — drop-in corrupted or /etc/containerd/config.d not imported; OFF timings "
            f"would falsely equal ON"
        )
        off_times = _timed_pulls(node, image, bench_ref)
        b.remove_containerd_dropin(node, image, BENCH_DROPIN)  # restore booted (ON) config

        _report(node, bench_ref, active, cri_transfer, warm, on_times, off_times)

        # Sanity, not a perf threshold: timings valid and ON not slower than OFF beyond noise.
        on_med, off_med = _median(on_times), _median(off_times)
        assert on_med > 0 and off_med > 0, f"pull timing failed (on={on_times}, off={off_times})"
        assert on_med <= off_med * 1.25, (
            f"parallel-pull ON ({on_med:.1f}s) unexpectedly slower than OFF ({off_med:.1f}s) — investigate"
        )
    finally:
        if node:
            b.remove_containerd_dropin(node, image, BENCH_DROPIN)
        run_kubectl("delete", "pod", PROBE, "-n", h.NAMESPACE, "--ignore-not-found", check=False)


def _assert_transfer_keys_active(node: str, image: str) -> dict[str, int]:
    """Assert the transfer-plugin keys appear in `containerd config dump` (the effective merged
    config, so this proves they are active, not merely written); return the parsed key->value map."""
    dump = b.containerd_config_dump(node, image)
    assert TRANSFER_PLUGIN in dump, (
        f"{node}: '{TRANSFER_PLUGIN}' absent from `containerd config dump` — parallel-pull keys "
        f"are inert on this containerd build.\n--- dump tail ---\n{dump[-1500:]}"
    )
    found: dict[str, int] = {}
    for key in TRANSFER_KEYS:
        m = re.search(rf"^\s*{re.escape(key)}\s*=\s*(\d+)", dump, re.MULTILINE)
        if m:
            found[key] = int(m.group(1))
    return found


def _cri_uses_transfer_service(node: str, image: str) -> bool:
    """Best-effort: does CRI route image pulls through the transfer service? containerd 2.x
    exposes this as use_local_image_pull/image_pull_with_sync (the speedup depends on it)."""
    dump = b.containerd_config_dump(node, image)
    return "use_local_image_pull = true" in dump or "image_pull_with_sync = true" in dump


def _timed_pulls(node: str, image: str, ref: str) -> list[float]:
    """REPEATS cold pulls (rmi + pull); drop any that failed (-1)."""
    return [t for t in (b.crictl_pull_seconds(node, image, ref) for _ in range(REPEATS)) if t > 0]


def _median(xs: list[float]) -> float:
    if not xs:
        return -1.0
    s = sorted(xs)
    return s[len(s) // 2]


def _bench_image_ref(e2e: EndToEndDeployment) -> str:
    """A large multi-layer image ref reachable from the node.

    Prefer an operator-supplied BENCH_IMAGE (any ref the air-gapped node can pull — a full ECR
    ref or a pull-through path). Otherwise onboard the vllm-qwen workload image (guaranteed large,
    multi-layer, and vendored into this cluster's ECR).
    """
    override = os.environ.get(BENCH_IMAGE_ENV, "").strip()
    if override:
        return override

    region = h.jd_output(e2e, "region")
    overrides = h.onboard_chart(e2e, region, "vllm-qwen")
    text = overrides.read_text()
    # The onboarder rewrites the image to <ecr>/<cluster>/workload/vllm/vllm-openai@sha256:...
    m = re.search(r"\S*workload/vllm/vllm-openai@sha256:[0-9a-f]+", text)
    assert m, f"could not find vendored vLLM image ref in overrides:\n{text}"
    return m.group(0)


def _report(
    node: str,
    ref: str,
    active: dict[str, int],
    cri_transfer: bool,
    warm: float,
    on_times: list[float],
    off_times: list[float],
) -> None:
    """Print the benchmark result table (visible with -s / --log-cli-level=INFO)."""
    on_med, off_med = _median(on_times), _median(off_times)
    speedup = (off_med / on_med) if on_med > 0 else float("nan")
    lines = [
        "",
        "=== GPU parallel-pull benchmark ===",
        f"node:             {node}",
        f"image:            {ref}",
        f"active config:    {active}  (CRI transfer service: {cri_transfer})",
        f"warmup pull (s):  {warm:.1f}  (cache-populating; excluded from comparison)",
        f"pull ON  (s):     {[round(t, 1) for t in on_times]}  median={on_med:.1f}",
        f"pull OFF (s):     {[round(t, 1) for t in off_times]}  median={off_med:.1f}",
        f"speedup (off/on): {speedup:.2f}x",
        "===================================",
    ]
    print("\n".join(lines))
