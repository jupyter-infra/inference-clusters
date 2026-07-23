"""Gated live E2E — Kueue gang scheduling on GPU g-tier nodes WITH EFA + same-AZ.

This is the single GPU gang-scheduling E2E: it is a strict SUPERSET of a plain
GPU gang test (2-pod LWS admitted by Kueue → gpu-g flavor injected → pods land on
Karpenter nvidia-g nodes → Running), plus the two things the EFA path adds — an
EFA interface per pod and same-AZ co-location. Since one 2-node g-tier cluster
proves the whole chain, we do NOT run a second (equally expensive) plain-gang
test; the g-tier node-label assertion below is exactly what that test checked.

Steps:
  1. Enable LWS + Kueue + EFA on the cluster
  2. Create a 2-pod LWS, each requesting nvidia.com/gpu: 1 + vpc.amazonaws.com/efa: 1,
     with the LWS exclusive-topology annotation pinning both pods to the same AZ
  3. Assert: Kueue Workload reaches Admitted=True
  4. Assert: both pods reach Running on Karpenter g-tier nodes (inference/accelerator=nvidia-g)
  5. Assert: each pod has an EFA interface allocated
  6. Assert: both pods land in the SAME AZ (exclusive-topology — EFA can't cross AZ)

Co-location is via the LWS exclusive-topology annotation on topology.kubernetes.io/zone
(the leader schedules first, then LWS stamps its zone as a nodeSelector on the workers —
a Karpenter-native pattern that works scale-from-zero), NOT Kueue TAS. TAS pre-computes
fit over existing nodes, which don't exist at admission time on Karpenter, and its
ProvisioningRequest path is Cluster-Autoscaler-only.

Why g-tier (not p5): Karpenter picks the smallest EFA-capable g instance (1 EFA
interface, validated g5.8xlarge ~$2/hr) vs p5.48xlarge (32 EFA, ~$98/hr,
frequently InsufficientCapacity). It proves the EFA + gang mechanism without the
p5 capacity flakiness; it does NOT prove the 32-rail p5 topology.

Marked `mutating` — enables operators + re-applies.
"""

import time

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl

from tests.e2e import _serving_helpers as h

LWS_NAME = "efa-gang-e2e"
NAMESPACE = "inference"
LOCAL_QUEUE = "inference"


@pytest.mark.mutating
def test_kueue_efa_multinode_gang(
    e2e_deployment: EndToEndDeployment,
    kubernetes_cluster_login: None,
) -> None:
    """EFA gang: Kueue admits, pods get EFA interfaces, exclusive-topology co-locates in one AZ."""
    # Enable LWS + Kueue + EFA (fixture already deployed the base cluster)
    e2e_deployment.update_override_value("enable_lws", True)
    e2e_deployment.update_override_value("enable_kueue", True)
    e2e_deployment.update_override_value("enable_efa", True)
    e2e_deployment.ensure_deployed_with([], timeout_seconds=1200)

    image = h.client_image(e2e_deployment)

    try:
        run_kubectl("create", "namespace", NAMESPACE, check=False)

        h.apply_resource(
            "efa-gang-lws.yaml",
            image=image,
            namespace=NAMESPACE,
            queue_name=LOCAL_QUEUE,
        )

        # Assert Kueue Workload reaches Admitted=True
        admitted = False
        for _ in range(30):
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
            workloads = run_kubectl("get", "workloads", "-n", NAMESPACE, "-o", "yaml", check=False).stdout
            raise AssertionError(
                f"Kueue Workload never reached Admitted=True for EFA gang\n--- Kueue workloads ---\n{workloads[-3000:]}"
            )

        # Wait for both pods Running (g6e.24xlarge provisioning can take a few min)
        all_ready = False
        for _ in range(42):  # ~7 min
            result = run_kubectl(
                "get",
                "pods",
                "-n",
                NAMESPACE,
                "-l",
                f"app={LWS_NAME}",
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
            desc = run_kubectl("describe", "pods", "-n", NAMESPACE, "-l", f"app={LWS_NAME}", check=False).stdout
            raise AssertionError(
                f"Expected 2 Running EFA pods, got phases: {phases}\n--- Pod describe ---\n{desc[-2000:]}"
            )

        # Assert both pods got an EFA interface allocated (limits reflect the request)
        efa_limits = (
            run_kubectl(
                "get",
                "pods",
                "-n",
                NAMESPACE,
                "-l",
                f"app={LWS_NAME}",
                "-o",
                r"jsonpath={.items[*].spec.containers[0].resources.limits.vpc\.amazonaws\.com/efa}",
                check=True,
            )
            .stdout.strip()
            .split()
        )
        assert efa_limits == ["1", "1"], f"Both pods must have 1 EFA interface allocated, got: {efa_limits}"

        # Assert pods landed on Karpenter g-tier GPU nodes (the gpu-g flavor) AND
        # exclusive-topology co-located both in the same AZ (EFA cannot cross AZ). The
        # g-tier check is what the retired plain-gang test asserted — kept here so
        # this single test still covers the full flavor-injection path.
        nodes = (
            run_kubectl(
                "get",
                "pods",
                "-n",
                NAMESPACE,
                "-l",
                f"app={LWS_NAME}",
                "-o",
                "jsonpath={.items[*].spec.nodeName}",
                check=True,
            )
            .stdout.strip()
            .split()
        )
        assert len(nodes) == 2, f"Expected 2 scheduled pods, got: {nodes}"

        for node in nodes:
            accelerator = run_kubectl(
                "get", "node", node, "-o", r"jsonpath={.metadata.labels.inference/accelerator}", check=True
            ).stdout.strip()
            assert accelerator == "nvidia-g", (
                f"Pod must run on a g-tier GPU node (gpu-g flavor), "
                f"but {node} has inference/accelerator={accelerator!r}"
            )

        zones = []
        for node in nodes:
            zone = run_kubectl(
                "get", "node", node, "-o", r"jsonpath={.metadata.labels.topology\.kubernetes\.io/zone}", check=True
            ).stdout.strip()
            zones.append(zone)
        assert zones[0] == zones[1] and zones[0], (
            f"exclusive-topology must co-locate both pods in the same AZ for EFA, got zones: {zones}"
        )

    finally:
        # Deleting the LWS cascades to owned pods (ownerReferences)
        run_kubectl(
            "delete",
            "leaderworkerset",
            LWS_NAME,
            "-n",
            NAMESPACE,
            "--ignore-not-found",
            check=False,
        )
