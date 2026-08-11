"""Live E2E for gpu_parallel_image_pull (containerd 2.2 parallel download/unpack on gpu/gpu-p).

Non-mutating: a GPU pod pulls+serves on a real node, then we read that node's containerd config.
Mutating: flip the flag off/on and assert the block enters gpu+gpu-p userData, never cpu.
"""

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl

from tests.e2e import _serving_helpers as h

# The containerd settings the feature writes: discard_unpacked_layers=false routes pod pulls
# through the transfer service (EKS defaults it true, which forces local mode), and the transfer
# plugin carries the raised concurrency. Values are the chart defaults (gpuParallelPull).
EXPECTED_CONTAINERD_SETTINGS = (
    "discard_unpacked_layers = false",
    "max_concurrent_downloads = 20",
    "concurrent_layer_fetch_buffer = 16777216",
    "max_concurrent_unpacks = 5",
)

PROBE = "gpu-parallel-pull-probe"  # matches metadata.name in gpu-parallel-pull-probe.yaml


def _ec2nodeclass_userdata(name: str) -> str:
    """spec.userData of a Karpenter EC2NodeClass (empty string if unset)."""
    return run_kubectl("get", "ec2nodeclass", name, "-o", "jsonpath={.spec.userData}", check=True).stdout


@pytest.mark.full_deployment
def test_parallel_pull_serves_and_config_applies_on_gpu_node(
    e2e_deployment: EndToEndDeployment,
    kubernetes_cluster_login: None,
) -> None:
    """A GPU pod pulls and serves on a Karpenter GPU node, and that node's containerd config
    carries the parallel-pull settings.

    A 1-GPU httpd pod with a readiness probe proves the image pulled and serves; reading the
    node's containerd config then proves the block was applied by nodeadm, not just declared.
    """
    e2e_deployment.ensure_deployed()
    image = h.client_image(e2e_deployment)

    try:
        h.apply_resource("gpu-parallel-pull-probe.yaml", image=image, namespace=h.NAMESPACE)
        # Ready gates on a successful pull + the readiness probe (scale-from-zero GPU node budget).
        ready = run_kubectl(
            "wait", f"pod/{PROBE}", "-n", h.NAMESPACE, "--for=condition=Ready", "--timeout=600s", check=False
        )
        if ready.returncode != 0:
            desc = run_kubectl("describe", "pod", PROBE, "-n", h.NAMESPACE, check=False).stdout
            raise AssertionError(
                f"GPU probe pod never became Ready (pull/serve failed?):\n{ready.stderr}\n{desc[-2000:]}"
            )

        # Explicit proof the image pulled: no waiting/ImagePullBackOff, container is Running.
        _assert_image_pulled(PROBE)

        node = h.assert_on_karpenter_gpu(PROBE)

        # Serve check: hit the pod's /ping from inside the container (air-gapped; no external LB).
        ping = h.exec_in_pod(h.NAMESPACE, PROBE, "wget", "-qO-", "http://127.0.0.1:8080/ping")
        assert ping.stdout.strip() == "ok", f"probe did not serve /ping; got {ping.stdout!r}"

        # Config check: the parallel-pull block was merged by nodeadm and applied on the node.
        config = _read_node_containerd_config(node, image)
        missing = [s for s in EXPECTED_CONTAINERD_SETTINGS if s not in config]
        assert not missing, f"GPU node {node} containerd config missing {missing}\n--- config ---\n{config[-2000:]}"
    finally:
        run_kubectl("delete", "pod", PROBE, "-n", h.NAMESPACE, "--ignore-not-found", check=False)


def _assert_image_pulled(pod: str) -> None:
    """The pod's container is Running with no image-pull error (the pull path succeeded)."""
    state = run_kubectl(
        "get", "pod", pod, "-n", h.NAMESPACE, "-o", "jsonpath={.status.containerStatuses[0].state}", check=True
    ).stdout
    assert '"running"' in state, f"probe container not Running (image pull failed?): {state}"
    assert "ImagePullBackOff" not in state and "ErrImagePull" not in state, f"image pull error: {state}"


def _read_node_containerd_config(node: str, image: str) -> str:
    """Effective (merged) containerd config on a node via `containerd config dump`.

    Uses the ECR pull-through busybox (nodes are air-gapped; a public.ecr.aws ref won't pull).
    config dump reflects nodeadm's merged userData, wherever it landed in the file tree.
    """
    debug = run_kubectl(
        "debug",
        f"node/{node}",
        f"--image={image}",
        "--",
        "chroot",
        "/host",
        "sh",
        "-c",
        "containerd config dump 2>/dev/null",
        check=False,
    )
    return debug.stdout


@pytest.mark.mutating
def test_parallel_pull_flag_toggles_gpu_userdata_only(
    e2e_deployment: EndToEndDeployment,
    kubernetes_cluster_login: None,
) -> None:
    """Flipping gpu_parallel_image_pull off→on adds the containerd block to gpu + gpu-p userData
    and never to cpu — the on/off contract at the EC2NodeClass layer (no node roll needed).

    Reverts to on (the default) at the end so the cluster returns to base state for later tests.
    """
    e2e_deployment.ensure_deployed()
    marker = "io.containerd.transfer.v1.local"

    try:
        # OFF: no GPU class carries the block.
        e2e_deployment.update_override_value("gpu_parallel_image_pull", False)
        e2e_deployment.ensure_deployed_with([], timeout_seconds=900)
        for cls in ("gpu", "gpu-p"):
            assert marker not in _ec2nodeclass_userdata(cls), f"{cls} userData should NOT have parallel-pull when off"

        # ON: gpu + gpu-p carry the block with the expected values; cpu never does.
        e2e_deployment.update_override_value("gpu_parallel_image_pull", True)
        e2e_deployment.ensure_deployed_with([], timeout_seconds=900)
        for cls in ("gpu", "gpu-p"):
            ud = _ec2nodeclass_userdata(cls)
            assert marker in ud, f"{cls} userData missing parallel-pull block when on"
            for setting in EXPECTED_CONTAINERD_SETTINGS:
                assert setting in ud, f"{cls} userData missing '{setting}'\n--- userData ---\n{ud}"
        assert marker not in _ec2nodeclass_userdata("cpu"), "cpu userData must NEVER carry the parallel-pull block"
    finally:
        # Restore the default (on) regardless of outcome.
        e2e_deployment.update_override_value("gpu_parallel_image_pull", True)
        e2e_deployment.ensure_deployed_with([], timeout_seconds=900)
