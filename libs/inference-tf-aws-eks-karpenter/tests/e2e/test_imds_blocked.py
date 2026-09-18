"""Live proof that pods cannot reach EC2 IMDS — the httpPutResponseHopLimit=1 guard.

A pod runs one network hop beyond the host, and IMDS stamps its responses with an IP TTL
equal to the hop limit, so at limit 1 the response expires before reaching the pod: it
cannot read instance metadata or assume the node role via SSRF. Workloads get AWS creds
from EKS Pod Identity instead (see test_batch_s3_access), so this costs them nothing.

Probes run on both launch paths — the Karpenter EC2NodeClasses (cpu, gpu-g) and the
bootstrap MNG launch template — because each sets the hop limit through a different
mechanism. gpu-p is covered statically by the unit test (identical config, capacity-gated
instances). A hostNetwork pod (hop 0) is the positive control: it MUST reach IMDS, pinning
the block to the hop limit rather than a route/firewall/image problem. busybox comes from
ECR pull-through (the endpoints-only VPC cannot pull from Docker Hub).
"""

import json

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl
from pytest_jupyter_deploy.kubernetes.namespace import delete_namespace, temporary_namespace

from tests.e2e import _serving_helpers as h

PROBE_NS = "e2e-imds"
IMDS_URL = "http://169.254.169.254/latest/meta-data/"
_WGET_TIMEOUT_S = 5
_HARD_TIMEOUT_S = 15

_GPU_TOLERATION = {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}
_SYSTEM_TOLERATION = {"key": "inference/role", "operator": "Equal", "value": "system", "effect": "NoSchedule"}

# One target per node launch path. gpu-g is marked gpu so conftest groups it into the
# warm-GPU block and it rides an existing GPU node instead of forcing a new one.
NODE_TARGETS = [
    pytest.param({"karpenter.sh/nodepool": "cpu"}, [], id="karpenter-cpu"),
    pytest.param({"inference/role": "system"}, [_SYSTEM_TOLERATION], id="mng-bootstrap"),
    pytest.param(
        {"inference/accelerator": "nvidia-g"}, [_GPU_TOLERATION], id="karpenter-gpu-g", marks=pytest.mark.gpu
    ),
]


def _run_probe(pod: str, image: str, node_selector: dict, tolerations: list, *, host_network: bool) -> None:
    """Launch a sleeping probe pod pinned to a node target and wait for it to be Ready."""
    overrides = {
        "spec": {
            "restartPolicy": "Never",
            "hostNetwork": host_network,
            "nodeSelector": node_selector,
            "tolerations": tolerations,
        }
    }
    run_kubectl("delete", "pod", pod, "-n", PROBE_NS, "--ignore-not-found", "--wait=false", check=False)
    run_kubectl(
        "run", pod, "-n", PROBE_NS, "--image", image, "--restart=Never",
        f"--overrides={json.dumps(overrides)}", "--command", "--", "sleep", "3600", check=True,
    )
    run_kubectl("wait", "--for=condition=Ready", f"pod/{pod}", "-n", PROBE_NS, "--timeout=300s", check=True)


def _imds_reachable(pod: str) -> tuple[bool, str]:
    """wget IMDS from the pod; reachable iff it answered at the HTTP layer (a 401 under IMDSv2)."""
    # busybox wget exits non-zero for a 401 too, so read reachability from output, not rc.
    command = f"timeout {_HARD_TIMEOUT_S} wget -T {_WGET_TIMEOUT_S} -O - {IMDS_URL} 2>&1"
    res = h.exec_in_pod(PROBE_NS, pod, "sh", "-c", command, check=False)
    out = f"{res.stdout}\n{res.stderr}"
    return (res.returncode == 0 or "401" in out), out


@pytest.fixture(scope="module")
def probe_namespace(e2e_deployment: EndToEndDeployment, kubernetes_cluster_login: None):
    """A throwaway namespace (no PSA labels, so the hostNetwork control pod is admitted)."""
    e2e_deployment.ensure_deployed()
    delete_namespace(PROBE_NS)
    run_kubectl("wait", "--for=delete", f"namespace/{PROBE_NS}", "--timeout=120s", check=False)
    with temporary_namespace(PROBE_NS):
        yield


@pytest.mark.full_deployment
@pytest.mark.parametrize("node_selector, tolerations", NODE_TARGETS)
def test_imds_blocked_from_pod(
    e2e_deployment: EndToEndDeployment, probe_namespace: None, node_selector: dict, tolerations: list, request
) -> None:
    """A normal pod on each node launch path must NOT reach IMDS (fails if hop limit is not 1)."""
    target = request.node.callspec.id
    pod = f"imds-probe-{target}"
    _run_probe(pod, h.client_image(e2e_deployment), node_selector, tolerations, host_network=False)
    reachable, out = _imds_reachable(pod)
    assert not reachable, f"a pod on {target} reached IMDS — httpPutResponseHopLimit is not 1:\n{out}"


@pytest.mark.full_deployment
def test_imds_reachable_from_host_network(e2e_deployment: EndToEndDeployment, probe_namespace: None) -> None:
    """Positive control: a hostNetwork pod (hop 0) MUST reach IMDS, so the blocks above are real."""
    _run_probe("imds-probe-hostnet", h.client_image(e2e_deployment), {}, [], host_network=True)
    reachable, out = _imds_reachable("imds-probe-hostnet")
    assert reachable, f"a hostNetwork pod could not reach IMDS — probe/endpoint broken:\n{out}"
