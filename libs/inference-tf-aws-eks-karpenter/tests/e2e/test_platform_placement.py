"""Gated live E2E — platform node placement + control-loop replica count.

Validates that platform pods (control-loop operators AND managed add-on controllers) are
pinned to the tainted system MNG (inference/role=system), never landing on a Karpenter
inference node — and that the leader-elected operators run 2 replicas (warm standby).

Placement matters because a nodeSelector-less controller with only a taint toleration is
merely PERMITTED on the system NG, not pinned to it — it could drift onto a future
untainted Karpenter pool. This test catches that silent regression.

Scope:
  - ALWAYS-ON operators (Karpenter, Cluster Autoscaler, KRO, KEDA): 2 replicas + placement.
    Flag-gated operators (Kueue, LWS) are covered by test_kueue_gang.py once enabled.
  - Managed add-on CONTROLLER Deployments (coredns, ebs-csi, s3-csi): placement only.
    NOT the DaemonSet parts (vpc-cni, kube-proxy, CSI node plugins, CloudWatch agent) —
    those run on every node by design, Karpenter GPU nodes included.
  - The kube-prometheus-stack release (operator, kube-state-metrics, Grafana, and the
    Prometheus/Alertmanager StatefulSet pods): placement only. The tolerate-all
    node-exporter DaemonSet + GPU-only DCGM are excluded (they run off the system MNG by
    design).

Non-mutating: reads the base deployment as-is. Marked `full_deployment` (no GPU).
"""

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment

from tests.e2e import _serving_helpers as h

# (namespace, helm_release instance label) for the always-on leader-elected operators.
# The KEDA operator + metrics-apiserver both live under the "keda" release; the stateless
# admission webhooks are intentionally NOT here (not leader-elected → not scaled for HA).
ALWAYS_ON_OPERATORS = [
    ("kube-system", "karpenter"),
    ("kube-system", "cluster-autoscaler"),
    ("kro", "kro"),
    ("keda", "keda"),
]

# Always-on monitoring workloads pinned to the system MNG in platform_prometheus.tf. Every
# kube-prometheus-stack pod (operator Deployment, kube-state-metrics, Grafana, AND the
# Prometheus/Alertmanager StatefulSet pods) carries the release instance label, so one
# selector covers the whole release — EXCEPT the node-exporter DaemonSet + DCGM, which run
# on every / on GPU nodes by design (tolerate-all) and are correctly NOT on the system MNG.
# node-exporter is filtered out below by excluding its component name.
MONITORING_RELEASE = ("monitoring", "kube-prometheus-stack")
# Pods in the monitoring release that legitimately run OFF the system MNG (tolerate-all
# DaemonSets that must scrape every node, GPU nodes included) — excluded from the check.
MONITORING_OFF_SYSTEM_SUBSTRINGS = ("node-exporter",)

# (namespace, label selector, description) for the managed add-on CONTROLLER Deployments
# pinned to the system NG via nodeSelector in eks_addons.tf. DaemonSets (vpc-cni,
# kube-proxy, the ebs/s3 CSI node plugins) are excluded — they run everywhere by design.
ADDON_CONTROLLERS = [
    ("kube-system", "k8s-app=kube-dns", "coredns"),
    ("kube-system", "app=ebs-csi-controller", "ebs-csi controller"),
    ("kube-system", "app=s3-csi-controller", "s3-csi controller"),
]


@pytest.mark.full_deployment
def test_platform_operators_run_two_replicas_on_system_mng(
    e2e_deployment: EndToEndDeployment,
    kubernetes_cluster_login: None,
) -> None:
    """Every always-on leader-elected operator runs 2 ready replicas, all on the system MNG."""
    e2e_deployment.ensure_deployed()

    for namespace, release in ALWAYS_ON_OPERATORS:
        deployments = h.deployment_names_by_instance(namespace, release)
        assert deployments, (
            f"no Deployments found for release '{release}' in {namespace} "
            f"(app.kubernetes.io/instance label missing or release not installed?)"
        )
        # The KEDA release ships a stateless admission-webhook Deployment alongside the
        # two leader-elected ones (operator + metrics-apiserver). It is deliberately NOT
        # scaled for HA (nothing to fail over), so exclude it from the 2-replica check.
        leader_elected = [d for d in deployments if "webhook" not in d]
        assert leader_elected, f"no leader-elected Deployments for release '{release}' (all webhooks?)"
        for deployment in leader_elected:
            h.assert_deployment_replicas_ready(namespace, deployment, expected=2)

        # Placement: every replica of the release (webhooks included) must sit on a
        # tainted system node — placement applies to all components, not just the HA ones.
        h.assert_pods_on_system_mng(namespace, release)


@pytest.mark.full_deployment
@pytest.mark.parametrize("namespace,selector,description", ADDON_CONTROLLERS)
def test_addon_controllers_on_system_mng(
    e2e_deployment: EndToEndDeployment,
    kubernetes_cluster_login: None,
    namespace: str,
    selector: str,
    description: str,
) -> None:
    """Each managed add-on CONTROLLER Deployment is pinned to the system MNG (not just tolerated)."""
    e2e_deployment.ensure_deployed()
    h.assert_pods_by_selector_on_system_mng(namespace, selector, f"{description} controller")


@pytest.mark.full_deployment
def test_monitoring_stack_on_system_mng(
    e2e_deployment: EndToEndDeployment,
    kubernetes_cluster_login: None,
) -> None:
    """Every kube-prometheus-stack pod (operator, kube-state-metrics, Grafana, and the
    Prometheus/Alertmanager StatefulSet pods) is pinned to the system MNG — closing the
    issue #14 gap for the monitoring operator. The tolerate-all node-exporter DaemonSet is
    excluded: it runs on every node by design (including GPU nodes) and MUST NOT be pinned."""
    e2e_deployment.ensure_deployed()

    namespace, release = MONITORING_RELEASE
    h.assert_pods_by_selector_on_system_mng(
        namespace,
        f"app.kubernetes.io/instance={release}",
        f"release {release}",
        exclude_name_substrings=MONITORING_OFF_SYSTEM_SUBSTRINGS,
    )
