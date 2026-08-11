"""Opt-in benchmark for gpu_parallel_image_pull (SOCI snapshotter parallel pull/unpack).

Validates and measures the feature the way a workload experiences it — a real kubelet pod pull:
  1. asserts the booted GPU node uses the SOCI snapshotter (the FastImagePull gate's effect);
  2. onboards a large image, evicts it, and times kubelet pulling it via the pod's `Pulled`
     event (kubelet's real CRI path).

The snapshotter is a bootstrap-time node setting, so timing is an absolute measurement (no
same-node on/off flip). The config assertion is hard; timing is informational. Excluded from
the e2e gate by the `benchmark` marker; run via `just bench-gpu-parallel-pull`.
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

PROBE = "gpu-parallel-pull-probe"
PULLER = "gpu-parallel-pull-timer"
# Onboard chart (images: only) whose large image gets vendored to this cluster's ECR.
BENCH_CHART = Path(__file__).resolve().parent / "charts" / "bench-image"
# containerd's CRI images plugin table (config schema v3). nodeadm's FastImagePull gate sets
# its snapshotter to "soci" (parallel pull/unpack) — the on-node effect this benchmark verifies.
CRI_IMAGES_PLUGIN = "io.containerd.cri.v1.images"


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
        assert _uses_soci_snapshotter(node, image), (
            f"{node}: CRI snapshotter is not 'soci' — the FastImagePull gate did not take effect"
        )

        # 3. Time a real kubelet pod pull on the SOCI-enabled node.
        _evict_image(node, image, bench_ref)
        pull_s = _time_pod_pull(node, bench_ref)
        assert pull_s > 0, f"pod pull of {bench_ref} did not report a Pulled event in time"

        _report(node, bench_ref, pull_s)
    finally:
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


def _uses_soci_snapshotter(node: str, image: str) -> bool:
    """Whether the node's effective containerd config sets the CRI images snapshotter to "soci".

    Scopes the match to the CRI images plugin table so an unrelated soci mention can't false-pass.
    """
    dump = h.node_shell(node, image, "containerd config dump 2>/dev/null")
    return (
        re.search(
            rf"\[plugins\.['\"]?{re.escape(CRI_IMAGES_PLUGIN)}['\"]?\].*?snapshotter\s*=\s*['\"]soci['\"]",
            dump,
            re.DOTALL,
        )
        is not None
    )


def _evict_image(node: str, image: str, ref: str) -> None:
    """Remove `ref` and prune its layer blobs so the next pull is a true cold pull.

    `images rm` only drops the reference; `content prune references` evicts the unreferenced
    blobs (without it a re-pull is a warm no-op).
    """
    h.node_shell(
        node,
        image,
        f"ctr -n k8s.io images rm {ref} >/dev/null 2>&1; ctr -n k8s.io content prune references >/dev/null 2>&1; true",
    )


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
