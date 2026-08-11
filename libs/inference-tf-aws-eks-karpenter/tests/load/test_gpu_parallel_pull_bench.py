"""Opt-in benchmark — GPU containerd parallel image pull, same-instance rolling comparison.

Measures the effect of gpu_parallel_image_pull (containerd 2.2 parallel download + unpack —
thenewstack.io/accelerating-eks-image-pulls) on a REAL cluster `gpu` node.

Method (same instance held constant, config rolled in place):
  1. Schedule a GPU probe pod → Karpenter provisions ONE real `gpu` node (the actual cluster
     def, whatever instance it picks).
  2. Correctness (the PR-review check): `containerd config dump` proves the three keys land
     under the ACTIVE io.containerd.transfer.v1.local table; report whether CRI uses the
     transfer service.
  3. Timing: cold `crictl pull` of a large multi-layer image with parallel-pull ON, then flip
     the SAME node's config OFF in place (concurrency=1) + restart containerd, re-measure.
     Instance type / AZ / EBS / NIC are identical across both numbers — only the config differs.
  4. Restore the node's config; report the delta.

NOT a pass/fail gate: it hard-asserts the config lands (correctness) but treats pull timing as
informational (pull time is too environment-dependent to threshold). Excluded from the e2e
suite by the `benchmark` marker; run via `just bench-gpu-parallel-pull`.
"""

import os
import re

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl

from tests.e2e import _serving_helpers as h
from tests.load import _bench_helpers as b

PROBE = "gpu-parallel-pull-probe"
# Repeat each cold pull to smooth out registry/network jitter; report the median.
REPEATS = 3
# The image to pull-time. Parallel download/unpack only helps a LARGE, MANY-layer image, and it
# must be reachable from the air-gapped node (this cluster's ECR / pull-through). Supply one that
# fits your cluster via BENCH_IMAGE; default onboards the vLLM workload image (large, multi-layer)
# to guarantee a representative ML image is present in ECR.
BENCH_IMAGE_ENV = "BENCH_IMAGE"


@pytest.mark.benchmark
def test_gpu_parallel_pull_benchmark(
    e2e_deployment: EndToEndDeployment,
    kubernetes_cluster_login: None,
) -> None:
    e2e_deployment.ensure_deployed()
    image = h.client_image(e2e_deployment)  # ECR pull-through busybox for the debug/probe pods

    # A large multi-layer image already vendored to this cluster's ECR. Onboarding the vllm-qwen
    # chart guarantees it's present; the ref is the digest-pinned workload image.
    bench_ref = _large_vendored_image_ref(e2e_deployment)
    node = ""

    try:
        # 1. One real `gpu` node via a probe pod.
        h.apply_resource("gpu-parallel-pull-probe.yaml", image=image, namespace=h.NAMESPACE)
        assert b.wait_pod_ready(h.NAMESPACE, PROBE, timeout_s=600), "probe pod never Ready (no GPU node?)"
        node = h.assert_on_karpenter_gpu(PROBE)

        # 2. Correctness: keys active under the transfer plugin (booted state = feature ON).
        active = b.assert_transfer_keys_active(node, image)
        assert active.get("max_concurrent_downloads") == 20, f"downloads not 20 in active config: {active}"
        assert active.get("max_concurrent_unpacks") == 5, f"unpacks not 5 in active config: {active}"
        cri_transfer = b.cri_uses_transfer_service(node, image)

        # 3. Timing on the SAME instance: ON (booted default) then OFF (rolled in place).
        on_times = _timed_pulls(node, image, bench_ref)
        b.set_parallel_pull(node, image, enabled=False)
        off_times = _timed_pulls(node, image, bench_ref)
        # Restore ON so the node matches the cluster default before we hand it back.
        b.set_parallel_pull(node, image, enabled=True)

        _report(node, bench_ref, active, cri_transfer, on_times, off_times)

        # Sanity only (NOT a threshold gate): a valid run produced timings, and enabling the
        # feature must not be SLOWER than disabling it beyond noise.
        on_med, off_med = _median(on_times), _median(off_times)
        assert on_med > 0 and off_med > 0, f"pull timing failed (on={on_times}, off={off_times})"
        assert on_med <= off_med * 1.25, (
            f"parallel-pull ON ({on_med:.1f}s) unexpectedly slower than OFF ({off_med:.1f}s) — investigate"
        )
    finally:
        if node:
            b.clear_bench_override(node, image)
        run_kubectl("delete", "pod", PROBE, "-n", h.NAMESPACE, "--ignore-not-found", check=False)


def _timed_pulls(node: str, image: str, ref: str) -> list[float]:
    """REPEATS cold pulls; drop any that failed (-1)."""
    return [t for t in (b.crictl_cold_pull_seconds(node, image, ref) for _ in range(REPEATS)) if t > 0]


def _median(xs: list[float]) -> float:
    if not xs:
        return -1.0
    s = sorted(xs)
    return s[len(s) // 2]


def _large_vendored_image_ref(e2e: EndToEndDeployment) -> str:
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
    on_times: list[float],
    off_times: list[float],
) -> None:
    """Print the benchmark result table (visible with -s / --log-cli-level=INFO)."""
    on_med, off_med = _median(on_times), _median(off_times)
    speedup = (off_med / on_med) if on_med > 0 else float("nan")
    lines = [
        "",
        "=== GPU parallel-pull benchmark ===",
        f"node:            {node}",
        f"image:           {ref}",
        f"active config:   {active}  (CRI transfer service: {cri_transfer})",
        f"pull ON  (s):    {[round(t, 1) for t in on_times]}  median={on_med:.1f}",
        f"pull OFF (s):    {[round(t, 1) for t in off_times]}  median={off_med:.1f}",
        f"speedup (off/on): {speedup:.2f}x",
        "===================================",
    ]
    print("\n".join(lines))
