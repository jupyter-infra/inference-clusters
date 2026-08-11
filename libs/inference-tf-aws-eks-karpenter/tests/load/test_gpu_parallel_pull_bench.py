"""Opt-in benchmark for gpu_parallel_image_pull (containerd transfer-service parallel pull).

Validates and measures the feature the way a workload experiences it — a real kubelet pod pull,
ON vs OFF on the SAME node (only the pull path changes, so hardware/AZ/registry are held constant):
  1. asserts the booted node routes pod pulls through the transfer service
     (discard_unpacked_layers=false) with the raised download concurrency (the feature's config);
  2. times kubelet pulling a large onboarded image via the pod's `Pulled` event (ON);
  3. flips the node to EKS-default local pull mode in place (containerd restart), re-times (OFF).

Config assertions are hard; timing is informational. Excluded from the e2e gate by the
`benchmark` marker; run via `just bench-gpu-parallel-pull`.
"""

import re
import subprocess
import time
from pathlib import Path

import pytest
import yaml
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl

from tests.e2e import _serving_helpers as h
from tests.load import _bench_helpers as b

PROBE = "gpu-parallel-pull-probe"
PULLER = "gpu-parallel-pull-timer"
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
        assert b.wait_pod_ready(h.NAMESPACE, PROBE, timeout_s=600), "probe pod never Ready (no GPU node?)"
        node = h.assert_on_karpenter_gpu(PROBE)

        # 2. Correctness: the booted node routes pod pulls through the transfer service (the
        #    feature's config), with the raised concurrency.
        assert b.uses_transfer_service(node, image), (
            f"{node}: discard_unpacked_layers is not false — pod pulls fell back to local mode, "
            f"so the transfer-service concurrency does not apply"
        )
        dl = b.transfer_max_downloads(node, image)
        assert dl == 20, f"{node}: transfer max_concurrent_downloads is {dl}, expected 20"

        # 3. ON: time a real kubelet pod pull on the booted (feature-on) node.
        b.evict_image(node, image, bench_ref)
        on_s = _time_pod_pull(node, bench_ref)
        assert on_s > 0, f"ON pod pull of {bench_ref} did not report a Pulled event in time"

        # 4. OFF: flip the SAME node to EKS-default local pull mode in place, re-measure.
        b.set_local_pull_fallback(node, image)
        assert not b.uses_transfer_service(node, image), "OFF flip did not take effect (still transfer mode)"
        b.evict_image(node, image, bench_ref)
        off_s = _time_pod_pull(node, bench_ref)
        b.clear_pull_override(node, image)  # restore booted (feature-on) config
        assert off_s > 0, f"OFF pod pull of {bench_ref} did not report a Pulled event in time"

        _report(node, bench_ref, dl, on_s, off_s)
    finally:
        if node:
            b.clear_pull_override(node, image)
        run_kubectl("delete", "pod", PULLER, "-n", h.NAMESPACE, "--ignore-not-found", check=False)
        run_kubectl("delete", "pod", PROBE, "-n", h.NAMESPACE, "--ignore-not-found", check=False)


def _time_pod_pull(node: str, ref: str) -> float:
    """Schedule a pod that pulls `ref` on `node`; return kubelet's reported pull seconds (-1 on timeout).

    Parses the kubelet `Pulled` event ("Successfully pulled image ... in <dur>"), which times the
    real CRI pull. do-not-disrupt keeps Karpenter from consolidating the node mid-measurement.
    """
    run_kubectl("delete", "pod", PULLER, "-n", h.NAMESPACE, "--ignore-not-found", check=False)
    manifest = (
        "apiVersion: v1\nkind: Pod\n"
        f"metadata: {{name: {PULLER}, namespace: {h.NAMESPACE}, "
        'annotations: {karpenter.sh/do-not-disrupt: "true"}}\n'
        "spec:\n  terminationGracePeriodSeconds: 2\n"
        f"  nodeName: {node}\n"
        "  tolerations: [{key: nvidia.com/gpu, operator: Exists, effect: NoSchedule}]\n"
        f'  containers: [{{name: c, image: {ref}, command: ["sh","-c","exit 0"]}}]\n'
    )
    subprocess.run(["kubectl", "apply", "-f", "-"], input=manifest, text=True, check=True, capture_output=True)
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
        secs = _parse_pull_duration(msg)
        if secs is not None:
            return secs
        time.sleep(5)
    return -1.0


def _parse_pull_duration(msg: str) -> float | None:
    """Parse kubelet's 'Successfully pulled image ... in 3m31.7s' into seconds; None if absent."""
    m = re.search(r"in ((?:\d+m)?[\d.]+m?s)", msg)
    if not m:
        return None
    text = m.group(1)
    mins = re.search(r"(\d+)m(?!s)", text)
    secs = re.search(r"([\d.]+)s", text)
    total = (int(mins.group(1)) * 60 if mins else 0) + (float(secs.group(1)) if secs else 0.0)
    return total or None


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


def _report(node: str, ref: str, downloads: int, on_s: float, off_s: float) -> None:
    """Print the benchmark result (visible with -s / --log-cli-level=INFO)."""
    speedup = (off_s / on_s) if on_s > 0 else float("nan")
    lines = [
        "",
        "=== GPU parallel-pull benchmark (same node) ===",
        f"node:               {node}",
        f"image:              {ref}",
        f"ON  (transfer, dl={downloads}):  {on_s:.1f}s",
        f"OFF (local, EKS default):  {off_s:.1f}s",
        f"speedup (off/on):   {speedup:.2f}x",
        "===============================================",
    ]
    print("\n".join(lines))
