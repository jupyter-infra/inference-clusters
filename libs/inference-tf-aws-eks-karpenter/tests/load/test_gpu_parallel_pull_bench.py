"""Opt-in benchmark for gpu_parallel_image_pull (SOCI snapshotter parallel pull/unpack).

Validates and measures the feature the way a workload experiences it — a real kubelet pod pull:
  1. asserts the booted GPU node uses the SOCI snapshotter (the FastImagePull gate's effect);
  2. onboards a large image, evicts it, and times kubelet pulling it via the pod's `Pulled`
     event (kubelet's real CRI path).

The snapshotter is a bootstrap-time node setting, so timing is an absolute measurement (no
same-node on/off flip). The config assertion is hard; timing is informational. Excluded from
the e2e gate by the `benchmark` marker; run via `just bench-gpu-parallel-pull`. Node-level and
parsing utilities live in `_loadtest_helpers.py` to keep this file orchestration-only.
"""

import subprocess
import time
from pathlib import Path

import pytest
import yaml
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl

from tests.e2e import _serving_helpers as h
from tests.load import _loadtest_helpers as lt

PROBE = "gpu-parallel-pull-probe"
PULLER = "gpu-parallel-pull-timer"  # matches metadata.name in resources/gpu-parallel-pull-timer.yaml
# Onboard chart (images: only) whose large image gets vendored to this cluster's ECR.
BENCH_CHART = Path(__file__).resolve().parent / "charts" / "bench-image"


@pytest.mark.benchmark
def test_gpu_parallel_pull_benchmark(
    e2e_deployment: EndToEndDeployment,
    kubernetes_cluster_login: None,
) -> None:
    e2e_deployment.ensure_deployed()
    image = h.client_image(e2e_deployment)  # ECR pull-through busybox for the debug pods
    bench_ref = _bench_image_ref(e2e_deployment)
    node = ""

    try:
        # 1. One real `gpu` node via a probe pod.
        h.apply_resource("gpu-parallel-pull-probe.yaml", image=image, namespace=h.NAMESPACE)
        ready = run_kubectl(
            "wait", f"pod/{PROBE}", "-n", h.NAMESPACE, "--for=condition=Ready", "--timeout=600s", check=False
        )
        assert ready.returncode == 0, "probe pod never Ready (no GPU node?)"
        node = h.assert_on_karpenter_gpu(PROBE)

        # 2. Correctness: the booted node uses the SOCI snapshotter (the feature's effect).
        assert lt.uses_soci_snapshotter(node, image), (
            f"{node}: CRI snapshotter is not 'soci' — the FastImagePull gate did not take effect"
        )

        # 3. Time a real kubelet pod pull on the SOCI-enabled node.
        lt.evict_image(node, image, bench_ref)
        pull_s = _time_pod_pull(node, bench_ref)
        assert pull_s > 0, f"pod pull of {bench_ref} did not report a Pulled event in time"

        _report(node, bench_ref, pull_s)
    finally:
        run_kubectl("delete", "pod", PULLER, "-n", h.NAMESPACE, "--ignore-not-found", check=False)
        run_kubectl("delete", "pod", PROBE, "-n", h.NAMESPACE, "--ignore-not-found", check=False)


def _time_pod_pull(node: str, ref: str) -> float:
    """Schedule a pod that pulls `ref` on `node`; return kubelet's reported pull seconds (-1 on timeout).

    Parses the kubelet `Pulled` event ("Successfully pulled image ... in <dur>"), which times the
    real CRI pull. The pod manifest (do-not-disrupt, nodeName pin) lives in resources/.
    """
    run_kubectl("delete", "pod", PULLER, "-n", h.NAMESPACE, "--ignore-not-found", check=False)
    lt.apply_load_resource("gpu-parallel-pull-timer.yaml", name=PULLER, namespace=h.NAMESPACE, node=node, ref=ref)
    for _ in range(180):  # up to ~15 min for a multi-GB cold pull
        msg = run_kubectl(
            "get",
            "event",
            "-n",
            h.NAMESPACE,
            "--field-selector",
            f"involvedObject.name={PULLER},reason=Pulled",
            "-o",
            "jsonpath={.items[0].message}",
            check=False,
        ).stdout
        secs = lt.parse_pull_duration(msg)
        if secs is not None:
            return secs
        time.sleep(5)
    return -1.0


def _bench_image_ref(e2e: EndToEndDeployment) -> str:
    """Vendor the bench-image chart's large image into this cluster's ECR and return its ref.

    The onboarder (which has public egress) digest-vendors the image so the air-gapped node can
    pull it over the VPC endpoint. Returns the full `<registry>/<repository>@sha256:...` ref.
    """
    region = h.jd_output(e2e, "region")
    in_uri = h.jd_output(e2e, "onboarder_input_s3_uri")
    staged = h._stage_fixture(BENCH_CHART)
    subprocess.run(["helm", "package", str(staged), "-d", "/tmp"], check=True, capture_output=True)
    tgz = next(Path("/tmp").glob("bench-image-*.tgz"))
    subprocess.run(["aws", "s3", "cp", str(tgz), f"{in_uri}/bench-image.tgz"], check=True, capture_output=True)
    overrides = h._run_onboard_build(e2e, region, "bench-image.tgz", "bench-image", "overrides.yaml")

    entry = yaml.safe_load(overrides.read_text())["images"]["bench"]
    # tag is "@sha256:..." for a vendored image, so it joins to repository with no separator.
    return f"{entry['registry']}/{entry['repository']}{entry['tag']}"


def _report(node: str, ref: str, pull_s: float) -> None:
    """Print the benchmark result (visible with -s / --log-cli-level=INFO)."""
    lines = [
        "",
        "=== GPU SOCI parallel-pull benchmark ===",
        f"node:              {node}",
        f"image:             {ref}",
        f"kubelet pod pull:  {pull_s:.1f}s  (SOCI snapshotter)",
        "====================================",
    ]
    print("\n".join(lines))
