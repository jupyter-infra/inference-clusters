"""Live E2E for gpu_parallel_image_pull (SOCI snapshotter parallel pull/unpack on gpu/gpu-p).

Non-mutating: a GPU pod pulls its image and serves on a real Karpenter GPU node — the feature is
applied on every GPU node, so a healthy pull+serve proves it does not break the pull path.
Mutating: flip the flag off/on and assert the FastImagePull gate enters gpu+gpu-p userData, never cpu.

(That the snapshotter is actually SOCI on the node is asserted by the benchmark, which reads the
node's effective containerd config; see tests/load/test_gpu_parallel_pull_bench.py.)
"""

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl

from tests.e2e import _serving_helpers as h

PROBE = "gpu-parallel-pull-probe"  # matches metadata.name in gpu-parallel-pull-probe.yaml


def _ec2nodeclass_userdata(name: str) -> str:
    """spec.userData of a Karpenter EC2NodeClass (empty string if unset)."""
    return run_kubectl("get", "ec2nodeclass", name, "-o", "jsonpath={.spec.userData}", check=True).stdout


@pytest.mark.gpu
@pytest.mark.full_deployment
def test_parallel_pull_serves_on_gpu_node(
    e2e_deployment: EndToEndDeployment,
    kubernetes_cluster_login: None,
) -> None:
    """A 1-GPU httpd pod pulls its image and serves /ping on a Karpenter GPU node.

    The feature is on for every GPU node, so a successful pull + serve proves it does not
    break the pull path (a broken snapshotter config would surface as ImagePullBackOff).
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

        h.assert_on_karpenter_gpu(PROBE)
        ping = h.exec_in_pod(h.NAMESPACE, PROBE, "wget", "-qO-", "http://127.0.0.1:8080/ping")
        assert ping.stdout.strip() == "ok", f"probe did not serve /ping; got {ping.stdout!r}"
    finally:
        run_kubectl("delete", "pod", PROBE, "-n", h.NAMESPACE, "--ignore-not-found", check=False)


@pytest.mark.mutating
def test_parallel_pull_flag_toggles_gpu_userdata_only(
    e2e_deployment: EndToEndDeployment,
    kubernetes_cluster_login: None,
) -> None:
    """Flipping gpu_parallel_image_pull off→on adds the FastImagePull gate to gpu + gpu-p userData
    and never to cpu — the on/off contract at the EC2NodeClass layer (no node roll needed).

    Reverts to on (the default) at the end so the cluster returns to base state for later tests.
    """
    e2e_deployment.ensure_deployed()
    marker = "FastImagePull"

    # Only the off-flip needs a guaranteed revert; the on-flip below doubles as the base-state restore.
    try:
        e2e_deployment.update_override_value("gpu_parallel_image_pull", False)
        e2e_deployment.ensure_deployed_with([], timeout_seconds=900)
        for cls in ("gpu", "gpu-p"):
            assert marker not in _ec2nodeclass_userdata(cls), f"{cls} userData should NOT have the gate when off"
    except BaseException:
        e2e_deployment.update_override_value("gpu_parallel_image_pull", True)
        e2e_deployment.ensure_deployed_with([], timeout_seconds=900)
        raise

    # ON: gpu + gpu-p carry the gate; cpu never does.
    e2e_deployment.update_override_value("gpu_parallel_image_pull", True)
    e2e_deployment.ensure_deployed_with([], timeout_seconds=900)
    for cls in ("gpu", "gpu-p"):
        ud = _ec2nodeclass_userdata(cls)
        assert marker in ud, f"{cls} userData missing the FastImagePull gate when on\n--- userData ---\n{ud}"
    assert marker not in _ec2nodeclass_userdata("cpu"), "cpu userData must NEVER carry the FastImagePull gate"
