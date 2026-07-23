"""E2E test that Kubernetes NetworkPolicy enforcement is actually ON.

The VPC CNI ships with its network-policy agent DISABLED by
default, which silently makes every NetworkPolicy inert — nothing is blocked. This
template turns it on via `enableNetworkPolicy=true` on the vpc-cni addon (see
engine/eks_addons.tf). A structural "the policy object exists" check would pass even
against an inert (unenforced) policy, so this test drives real traffic with a probe pod.

The test is self-contained: it stands up a target Service, applies a policy
that allows ONE labeled source, then proves BOTH directions with a probe pod:
  - a labeled source connects   (allow rule honored)
  - an unlabeled source times out (default-deny enforced — this is the assertion that
    fails if enableNetworkPolicy were missing)

The probe here is bespoke rather than using the pytest plugin utility because
that helper hardcodes curlimages/curl from Docker Hub, which the endpoints-only VPC
cannot pull. This probe uses the same ECR pull-through busybox the serving tests use.
"""

import json
import time

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl
from pytest_jupyter_deploy.kubernetes.namespace import delete_namespace, temporary_namespace

from tests.e2e import _serving_helpers as h

# Throwaway namespace the target + probes live in (created/deleted per test).
TARGET_NS = "e2e-netpol"

# Label the allow rule (resources/netpol-allow-labeled.yaml) keys on.
ALLOWED_LABEL = {"netpol-role": "allowed"}

# busybox exit 1 from wget on a timed-out connect; a clean fetch exits 0. wget has no
# dedicated timeout exit code (unlike curl's 28), so we bound the connect with -T and
# read "denied" as any non-zero exit, "allowed" as zero.
_CONNECT_TIMEOUT_S = 8


def _apply_target(image: str) -> None:
    """Stand up the httpd target Pod + Service and wait for it to be Ready."""
    h.apply_resource("netpol-target.yaml", TARGET_NS=TARGET_NS, TARGET_IMAGE=image)
    run_kubectl("wait", "--for=condition=Ready", "pod/netpol-target", "-n", TARGET_NS, "--timeout=120s", check=True)


def _probe_target(image: str, *, labeled: bool) -> bool:
    """Run a one-shot busybox wget at the target Service; return True iff it connected.

    A NetworkPolicy denial manifests as a connect timeout (non-zero exit); an allowed
    source fetches index.html (exit 0). The probe pod optionally carries the allow
    label so the SAME target policy exercises both the allow and deny paths.
    """
    pod = "netpol-probe-allowed" if labeled else "netpol-probe-denied"
    url = f"http://netpol-target.{TARGET_NS}.svc:8080/"
    overrides: dict = {"spec": {"restartPolicy": "Never"}}
    if labeled:
        overrides["metadata"] = {"labels": ALLOWED_LABEL}

    run_kubectl("delete", "pod", pod, "-n", TARGET_NS, "--ignore-not-found", "--wait=false", check=False)
    run_kubectl(
        "run",
        pod,
        "-n",
        TARGET_NS,
        "--image",
        image,
        "--restart=Never",
        f"--overrides={json.dumps(overrides)}",
        "--command",
        "--",
        "wget",
        "-q",
        "-O",
        "/dev/null",
        "-T",
        str(_CONNECT_TIMEOUT_S),
        url,
        check=True,
    )
    try:
        # Poll the container's OWN terminated exit code (kubectl run's rc is unreliable).
        # A bare `kubectl wait --for=jsonpath={.status.phase}` returns the instant the
        # field is non-empty — i.e. at Pending — so it does NOT mean the container has
        # exited; poll the terminated exitCode directly until it appears, like the plugin's
        # network_probe helper does.
        jsonpath = "{.status.containerStatuses[0].state.terminated.exitCode}"
        deadline = time.monotonic() + 90
        code = ""
        while time.monotonic() < deadline:
            code = run_kubectl(
                "get", "pod", pod, "-n", TARGET_NS, "-o", f"jsonpath={jsonpath}", check=True
            ).stdout.strip()
            if code != "":
                break
            time.sleep(2)
        assert code != "", f"probe pod '{pod}' did not terminate within 90s"
        return int(code) == 0
    finally:
        run_kubectl("delete", "pod", pod, "-n", TARGET_NS, "--ignore-not-found", "--wait=false", check=False)


@pytest.mark.full_deployment
def test_network_policy_is_enforced(e2e_deployment: EndToEndDeployment, kubernetes_cluster_login: None) -> None:
    """A default-deny-with-one-allow policy is actually enforced by the VPC CNI.

    The deny assertion is the real point: it fails (source reaches the target) if the
    vpc-cni addon were missing enableNetworkPolicy=true. The allow assertion is the
    positive control that pins the deny to the policy, not to an unreachable target.

    kubernetes_cluster_login (plugin fixture) does `jd cluster login` once per session.
    """
    image = h.client_image(e2e_deployment)

    # temporary_namespace deletes with --wait=false, so a prior run's namespace may still
    # be Terminating; delete-and-wait first so the create below never hits AlreadyExists.
    delete_namespace(TARGET_NS)
    run_kubectl("wait", "--for=delete", f"namespace/{TARGET_NS}", "--timeout=120s", check=False)

    with temporary_namespace(TARGET_NS):
        _apply_target(image)
        h.apply_resource("netpol-allow-labeled.yaml", TARGET_NS=TARGET_NS)

        # Positive control: the labeled source is on the allow-list, so it must connect.
        # The VPC CNI programs a fresh pod's label-based allow rule asynchronously and can
        # fail closed on the first try, so retry until it converges on the steady-state allow.
        allowed = any(_probe_target(image, labeled=True) for _ in range(5))
        assert allowed, "a pod carrying the allow label should reach the target on 8080"

        # The assertion that proves enforcement is ON: an unlabeled source is default-denied.
        # Default-deny is immediate and stable (no async allow rule to wait on), so one probe.
        denied = not _probe_target(image, labeled=False)
        assert denied, (
            "an unlabeled source reached the target on 8080 — NetworkPolicy is NOT enforced "
            "(is enableNetworkPolicy=true set on the vpc-cni addon?)"
        )
