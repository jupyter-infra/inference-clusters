"""Gated live E2E — an EC2NodeClass can be fully deleted (its GC finalizer completes).

Regression guard for the Karpenter instance-profile-GC wedge the unit tests cannot reach.
Karpenter's instance-profile GC controller calls iam:ListInstanceProfiles on a timer even
with a pre-created spec.instanceProfile. In our endpoints-only VPC (no IAM VPC endpoint —
IAM endpoints exist only in us-east-1/cn-north-1/us-gov-west-1) that call has no route, so
an EC2NodeClass's karpenter.k8s.aws/termination finalizer can never complete: a deleted
NodeClass wedges forever (deletionTimestamp set) and drags its whole NodePool to
NodeClassTerminating / NotReady — no nodes provision.

Two changes make deletion complete cleanly, one per egress posture:
- default (endpoints-only): settings.isolatedVPC=true on the Karpenter release de-registers
  the GC controller entirely (Karpenter >=1.8.3, aws/karpenter-provider-aws#8617) — zero IAM
  calls, so the finalizer completes trivially.
- NAT posture: the GC controller runs and reaches IAM over NAT; the iam.tf read-grant
  (iam:ListInstanceProfiles / GetInstanceProfile — Karpenter's own default policy) authorizes it.

What this asserts (posture-agnostic, the durable symptom): apply a throwaway NodeClass that
NO NodePool references (so no node is ever created/drained — purely the reconcile + GC
path), delete it, and require the object to be fully GONE within a bounded window. On the
pre-fix template it wedges (finalizer never completes) → fails. With either fix it deletes
cleanly → passes. We do not scrape the internal 403, which is version/log-format dependent.

Marked full_deployment — needs a live cluster; mutates nothing that isn't cleaned up here.
"""

import time

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl

from tests.e2e import _serving_helpers as h

PROBE = "e2e-nodeclass-gc-probe"
_DELETE_TIMEOUT_S = 120


def _nodeclass_exists(name: str) -> bool:
    """True while the EC2NodeClass object is still present (incl. wedged-Terminating)."""
    r = run_kubectl("get", "ec2nodeclass", name, "--ignore-not-found", "-o", "name", check=False)
    return bool(r.stdout.strip())


def _force_remove_finalizers(name: str) -> None:
    """Break a wedged deletion by clearing the finalizer directly (API-server delete, no IAM).

    Only reached when the assertion already failed (old policy), so the suite never leaves a
    wedged NodeClass behind to poison later runs / other NodePools.
    """
    run_kubectl(
        "patch", "ec2nodeclass", name, "--type=merge", "-p", '{"metadata":{"finalizers":[]}}', check=False
    )
    run_kubectl("delete", "ec2nodeclass", name, "--ignore-not-found", "--wait=false", check=False)


@pytest.mark.full_deployment
def test_nodeclass_deletion_finalizer_completes(
    e2e_deployment: EndToEndDeployment,
    kubernetes_cluster_login: None,
) -> None:
    """A deleted EC2NodeClass is fully garbage-collected (finalizer completes) within the window."""
    e2e_deployment.ensure_deployed()
    cluster_name = h.jd_output(e2e_deployment, "cluster_name")

    try:
        # 1. Create a NodeClass no NodePool references (no nodes involved) and let it reconcile
        #    to Ready — proves the reconcile-path IAM reads (GetInstanceProfile) succeed.
        h.apply_resource("nodeclass-gc-probe.yaml", cluster_name=cluster_name)
        ready = False
        for _ in range(24):  # ~2 min for first reconcile
            cond = run_kubectl(
                "get", "ec2nodeclass", PROBE,
                "-o", "jsonpath={.status.conditions[?(@.type==\"Ready\")].status}",
                check=False,
            ).stdout.strip()
            if cond == "True":
                ready = True
                break
            time.sleep(5)
        assert ready, (
            f"EC2NodeClass {PROBE} never reached Ready — its reconcile likely failed "
            f"(iam:GetInstanceProfile/ListInstanceProfiles denied?)."
        )

        # 2. Delete it. --wait=false: we poll ourselves so a wedged finalizer surfaces as a
        #    timeout with diagnostics rather than an opaque kubectl hang.
        run_kubectl("delete", "ec2nodeclass", PROBE, "--wait=false", check=True)

        # 3. Require full GC (finalizer completed → object gone) within the window. The old
        #    policy wedges here: the object keeps its deletionTimestamp forever.
        for _ in range(_DELETE_TIMEOUT_S // 5):
            if not _nodeclass_exists(PROBE):
                break
            time.sleep(5)
        else:
            karpenter_logs = run_kubectl(
                "logs", "-n", "kube-system", "-l", "app.kubernetes.io/name=karpenter",
                "--tail", "40", check=False,
            ).stdout
            raise AssertionError(
                f"EC2NodeClass {PROBE} was not garbage-collected within {_DELETE_TIMEOUT_S}s — "
                f"its termination finalizer never completed. The instance-profile GC controller "
                f"is calling iam:ListInstanceProfiles and can't reach IAM. Expected fixes: "
                f"settings.isolatedVPC=true (endpoints-only posture — de-registers the GC "
                f"controller) or the iam.tf ListInstanceProfiles/GetInstanceProfile grant (NAT "
                f"posture).\n--- karpenter logs ---\n{karpenter_logs[-2000:]}"
            )
    finally:
        # Never leave a wedged probe behind (a lingering NodeClass would fail later runs).
        if _nodeclass_exists(PROBE):
            _force_remove_finalizers(PROBE)
