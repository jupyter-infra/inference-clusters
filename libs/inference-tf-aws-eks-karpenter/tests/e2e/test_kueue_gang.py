"""Mutating live E2E — Kueue + LWS gang scheduling (CPU and GPU tracks).

These tests exercise the multi-node feature set by flipping enable_kueue/enable_lws on
a base cluster, reapplying, and asserting Kueue admits a gang-scheduled LeaderWorkerSet
atomically. Two genuinely distinct scenarios share one enable/revert cycle:

  - CPU track: 2-pod LWS requesting CPU only → default Karpenter NodePool.
  - GPU track: 2-pod LWS requesting 1 GPU each → g-tier ResourceFlavor injection +
    Karpenter GPU-node placement.

MUTATE-ONCE-AND-REVERT: enabling the operators is a slow reapply, so a module-scoped
fixture does it a SINGLE time for both tests, then reverts (enable_*=false, reapply) at
module teardown so the cluster returns to its base state for any later test/session reuse.
Both tests are `mutating` (they change infra config); the fixture — not the tests — owns
the state change, so the tests themselves are order-independent reads against the mutated
cluster.
"""

import time
from collections.abc import Generator

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl

from tests.e2e import _serving_helpers as h

NAMESPACE = "inference"
LOCAL_QUEUE = "inference"


@pytest.fixture(scope="module")
def kueue_lws_enabled(e2e_deployment: EndToEndDeployment) -> Generator[EndToEndDeployment, None, None]:
    """Enable Kueue+LWS once for the module, then revert to base state at teardown.

    The enable and the revert are each a full reconfigure+reapply, so this runs exactly
    twice per module regardless of how many tests consume it. The revert is in a finally
    so a mid-test failure still restores the base cluster."""
    e2e_deployment.ensure_deployed()
    e2e_deployment.update_override_value("enable_lws", True)
    e2e_deployment.update_override_value("enable_kueue", True)
    e2e_deployment.ensure_deployed_with([], timeout_seconds=900)
    try:
        yield e2e_deployment
    finally:
        # Revert so the cluster is back to base for any subsequent test/session reuse.
        e2e_deployment.update_override_value("enable_lws", False)
        e2e_deployment.update_override_value("enable_kueue", False)
        e2e_deployment.ensure_deployed_with([], timeout_seconds=900)


def _assert_operators_ha_on_system_mng() -> None:
    """Kueue + LWS controllers each run 2 ready replicas, all on the tainted system MNG.

    Folded in from the retired test_kueue_mutating: proves the operators not only exist
    but are HA (2 replicas) and correctly placed — the same properties test_platform_placement
    checks for the always-on operators, applied to the flag-gated ones once enabled."""
    for namespace, release in (("kueue-system", "kueue"), ("lws-system", "lws")):
        deployments = h.deployment_names_by_instance(namespace, release)
        assert deployments, f"no Deployments for release '{release}' in {namespace} after enabling"
        for deployment in deployments:
            h.assert_deployment_replicas_ready(namespace, deployment, expected=2)
        h.assert_pods_on_system_mng(namespace, release)


def _await_admitted_and_running(lws_name: str, pod_wait_polls: int) -> list[str]:
    """Assert Kueue admits the workload, both pods reach Running; return their node names."""
    admitted = False
    for _ in range(30):  # ~5 min
        result = run_kubectl(
            "get",
            "workloads",
            "-n",
            NAMESPACE,
            "-o",
            "jsonpath={.items[0].status.conditions[?(@.type=='Admitted')].status}",
            check=False,
        )
        if result.stdout.strip() == "True":
            admitted = True
            break
        time.sleep(10)

    if not admitted:
        workloads = run_kubectl("get", "workloads", "-n", NAMESPACE, "-o", "wide", check=False).stdout
        raise AssertionError(f"Kueue Workload never reached Admitted=True\n--- workloads ---\n{workloads}")

    all_ready = False
    phases: list[str] = []
    for _ in range(pod_wait_polls):
        result = run_kubectl(
            "get",
            "pods",
            "-n",
            NAMESPACE,
            "-l",
            f"app={lws_name}",
            "-o",
            "jsonpath={.items[*].status.phase}",
            check=False,
        )
        phases = result.stdout.strip().split()
        if len(phases) == 2 and all(p == "Running" for p in phases):
            all_ready = True
            break
        time.sleep(10)

    if not all_ready:
        desc = run_kubectl("describe", "pods", "-n", NAMESPACE, "-l", f"app={lws_name}", check=False).stdout
        raise AssertionError(f"Expected 2 Running pods, got phases: {phases}\n--- describe ---\n{desc[-2000:]}")

    return (
        run_kubectl(
            "get",
            "pods",
            "-n",
            NAMESPACE,
            "-l",
            f"app={lws_name}",
            "-o",
            "jsonpath={.items[*].spec.nodeName}",
            check=True,
        )
        .stdout.strip()
        .split()
    )


@pytest.mark.mutating
def test_kueue_gang_schedules_lws_group_cpu(
    kueue_lws_enabled: EndToEndDeployment,
    kubernetes_cluster_login: None,
) -> None:
    """Kueue admits a 2-pod CPU LWS atomically; Karpenter provisions nodes; both pods Running.

    Proves the integration path: Kueue admits -> pods Pending -> Karpenter launches nodes
    -> pods schedule. Also asserts the operators themselves are HA + on the system MNG.
    """
    _assert_operators_ha_on_system_mng()

    image = h.client_image(kueue_lws_enabled)
    lws_name = "gang-e2e"  # matches metadata.name in gang-scheduling-lws.yaml
    try:
        # The workload namespace is created by the engine (kubernetes_namespace_v1.workload).
        h.apply_resource("gang-scheduling-lws.yaml", image=image, namespace=NAMESPACE, queue_name=LOCAL_QUEUE)
        nodes = _await_admitted_and_running(lws_name, pod_wait_polls=18)  # ~3 min
        assert len(nodes) == 2, f"Expected 2 scheduled pods, got: {nodes}"
    finally:
        run_kubectl("delete", "leaderworkerset", lws_name, "-n", NAMESPACE, "--ignore-not-found", check=False)


@pytest.mark.mutating
def test_kueue_gang_schedules_on_gpu_nodes(
    kueue_lws_enabled: EndToEndDeployment,
    kubernetes_cluster_login: None,
) -> None:
    """Kueue admits a 2-pod GPU LWS; pods land on Karpenter g-tier nodes.

    Exercises the GPU path end-to-end: Kueue admits on GPU quota -> leader schedules ->
    Karpenter provisions a g-tier node -> LWS pins the worker to the leader's AZ
    (exclusive-topology) -> Karpenter provisions the worker's node -> both Running. Uses
    g-tier (not p-tier) to avoid ICE on scarce H100 capacity. The budget is generous
    because TWO GPU nodes provision sequentially (leader's, then the worker's).
    """
    image = h.client_image(kueue_lws_enabled)
    lws_name = "gang-gpu-e2e"  # matches metadata.name in gang-scheduling-gpu-lws.yaml
    try:
        # The workload namespace is created by the engine (kubernetes_namespace_v1.workload).
        h.apply_resource("gang-scheduling-gpu-lws.yaml", image=image, namespace=NAMESPACE, queue_name=LOCAL_QUEUE)
        nodes = _await_admitted_and_running(lws_name, pod_wait_polls=60)  # ~10 min (2 sequential GPU nodes)
        assert len(nodes) == 2, f"Expected 2 scheduled pods, got: {nodes}"
        for node in nodes:
            labels = run_kubectl("get", "node", node, "-o", "jsonpath={.metadata.labels}", check=True).stdout
            assert "nvidia" in labels, f"Pod must run on a Karpenter GPU node, but {node} labels lack nvidia"
    finally:
        run_kubectl("delete", "leaderworkerset", lws_name, "-n", NAMESPACE, "--ignore-not-found", check=False)
