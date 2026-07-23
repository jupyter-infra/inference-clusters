"""Gated live E2E — Cluster Autoscaler scales the system MNG up on Pending pods.

This is the end-to-end proof of issue #15 that the unit test cannot reach: the unit test
only checks the .tf SETS the discovery tags / IAM / helm release. Only a live cluster
proves the whole chain works — the ASG-tag propagation fix (MNG tags don't reach the ASG),
the Pod Identity IAM, and CA's SetDesiredCapacity call actually growing the tagged ASG.

Mechanism: apply a "ballast" Deployment pinned to the system MNG (toleration + nodeSelector)
whose CPU requests, spread one-per-node, exceed the free capacity on the current system
nodes → the surplus pods go Pending → CA grows the tagged system ASG → new node(s) join →
pods schedule. We assert the system-node count rises (bounded by bootstrap_max_size).

The ballast Deployment is created by the pytest-jupyter-deploy `ballast_deployment` helper
(a context manager that renders the one-per-node topology-spread + sleeper pods and deletes
them on exit), so this test only supplies the placement/sizing and the poll loop.

Scope: scale-UP only. Scale-down is deliberately NOT tested — CA's default
scale-down-unneeded-time is 10 min and system-cluster-critical pods / PDBs can legitimately
block it, so a scale-down assertion is slow and flaky. The context manager deletes the
ballast on exit, letting the cluster return to its floor on its own afterward.

Marked `full_deployment` — grows the ASG on a live cluster (no GPU needed); self-reverts by
deleting the ballast.
"""

import math
import time

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.kubernetes.ballast import ballast_deployment
from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl

from tests.e2e import _serving_helpers as h

NAMESPACE = "default"
BALLAST_APP = "ca-ballast"

# The system MNG's taint (inference/role=system:NoSchedule) — the ballast must tolerate it
# AND nodeSelect the same label, so it lands on (and only grows) the system pool.
_SYSTEM_NODE_SELECTOR = {"inference/role": "system"}
_SYSTEM_TOLERATION = {"key": "inference/role", "operator": "Equal", "value": "system", "effect": "NoSchedule"}


@pytest.mark.full_deployment
def test_cluster_autoscaler_scales_up_system_mng(
    e2e_deployment: EndToEndDeployment,
    kubernetes_cluster_login: None,
) -> None:
    """A Pending system-tier workload makes CA grow the tagged system ASG (a new node joins)."""
    e2e_deployment.ensure_deployed()

    start_nodes = h.system_node_names()
    start_count = len(start_nodes)
    assert start_count >= 1, "expected at least one system node at the floor"

    # Size each ballast pod at ~60% of a node's allocatable CPU so two can't co-locate
    # (topology spread already forces one-per-node). Request one MORE pod than there are
    # current nodes, so at least one pod is unschedulable until CA adds a node.
    per_node_cpu = h.system_node_allocatable_cpu_millicores()
    cpu_request = f"{math.floor(per_node_cpu * 0.6)}m"
    replicas = start_count + 1

    with ballast_deployment(
        name=BALLAST_APP,
        namespace=NAMESPACE,
        image=h.client_image(e2e_deployment),
        replicas=replicas,
        cpu_request=cpu_request,
        node_selector=_SYSTEM_NODE_SELECTOR,
        tolerations=[_SYSTEM_TOLERATION],
    ):
        # CA scan interval + node provision + join: poll up to ~8 min for the node count
        # to exceed the starting count (bounded above by bootstrap_max_size).
        grew = False
        current = start_count
        for _ in range(48):  # ~8 min
            current = len(h.system_node_names())
            if current > start_count:
                grew = True
                break
            time.sleep(10)

        if not grew:
            pending = run_kubectl(
                "get", "pods", "-n", NAMESPACE, "-l", f"app={BALLAST_APP}", "-o", "wide", check=False
            ).stdout
            ca_logs = run_kubectl(
                "logs",
                "-n",
                "kube-system",
                "-l",
                "app.kubernetes.io/instance=cluster-autoscaler",
                "--tail",
                "40",
                check=False,
            ).stdout
            raise AssertionError(
                f"system node count did not grow past {start_count} within ~8m "
                f"(still {current}) — CA did not scale up the tagged ASG.\n"
                f"--- ballast pods ---\n{pending}\n--- CA logs ---\n{ca_logs[-2000:]}"
            )
    # The context manager deletes the ballast; the ASG returns to its floor on its own after.
