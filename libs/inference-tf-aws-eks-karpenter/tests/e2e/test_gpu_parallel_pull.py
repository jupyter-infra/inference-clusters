"""Live E2E — GPU-node containerd parallel image pull (thenewstack.io/accelerating-eks-image-pulls).

The gpu_parallel_image_pull flag injects a containerd 2.2 parallel download+unpack block into
the gpu/gpu-p EC2NodeClass userData ONLY (CPU nodes pull small images and are excluded). Two
complementary tests:

  - Non-mutating (full_deployment): schedule a GPU pod, read the real node's containerd config,
    prove the parallel-pull settings actually took effect on the running node.
  - Mutating: flip the flag off then on, reapply, and assert the block leaves/enters the
    gpu + gpu-p EC2NodeClass userData and is NEVER present on the cpu class — the on/off contract.

The concurrency values are the AWS-recommended defaults baked into the chart, not jd-tunable.
"""

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl

from tests.e2e import _serving_helpers as h

# The three containerd keys the chart writes under [plugins.'io.containerd.transfer.v1.local'].
# Values are the fixed AWS-recommended defaults (charts/karpenter/values.yaml gpuParallelPull).
EXPECTED_CONTAINERD_SETTINGS = (
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
    """A GPU pod pulls + serves on a Karpenter GPU node, and that node's containerd config
    carries the parallel-pull settings.

    Schedules a single 1-GPU pod so Karpenter provisions a g-tier `gpu` node. The pod runs an
    httpd with a readiness probe, so pod-Ready proves the image PULLED successfully (the path
    this feature accelerates) AND the container can serve traffic — which we then confirm by
    hitting /ping. Finally reads /etc/containerd/config.toml on the node (via `kubectl debug
    node`) to prove the parallel-pull block was merged by nodeadm and applied, not just declared.
    """
    e2e_deployment.ensure_deployed()
    image = h.client_image(e2e_deployment)

    try:
        h.apply_resource("gpu-parallel-pull-probe.yaml", image=image, namespace=h.NAMESPACE)
        # Ready gates on a successful pull + the httpd readiness probe passing. Generous budget
        # for a scale-from-zero GPU node (provision + pull + start).
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

        # Landed on a Karpenter g-tier GPU node (shared helper; returns the node name).
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
    """Effective containerd config on a node, via a host-root debug pod (best-effort paths).

    Uses the ECR pull-through busybox (nodes are air-gapped; a public.ecr.aws ref won't pull).
    """
    debug = run_kubectl(
        "debug",
        f"node/{node}",
        "-it",
        f"--image={image}",
        "--",
        "chroot",
        "/host",
        "sh",
        "-c",
        # nodeadm merges into a drop-in; concatenate the known locations so we see the merged set.
        "cat /etc/containerd/config.toml /etc/containerd/config.d/*.toml 2>/dev/null",
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
