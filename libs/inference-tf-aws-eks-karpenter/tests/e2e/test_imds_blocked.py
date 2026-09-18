"""E2E test that a pod CANNOT reach EC2 IMDS — the httpPutResponseHopLimit=1 guard.

Every node launch path (the bootstrap MNG launch template in modules/node_group and all
Karpenter EC2NodeClasses) sets httpTokens=required + httpPutResponseHopLimit=1. A pod runs
one network hop beyond the host, and the IMDS service stamps its responses with an IP TTL
equal to the hop limit — so with a limit of 1 the response expires in transit and never
reaches the pod namespace. A pod therefore cannot read instance metadata or assume the node
role via SSRF. Workloads that need AWS get scoped credentials from EKS Pod Identity instead
(see test_batch_s3_access), whose on-host agent serves creds at a normal TTL and is
unaffected by the hop limit — so this block costs legitimate workloads nothing.

A structural "hop limit == 1 in the manifest" check would not prove the packet actually
dies, so this drives real traffic from two probe pods:
  - a normal pod (one hop beyond the host) MUST NOT reach IMDS — the assertion that fails
    if the hop limit ever regressed to 2.
  - a hostNetwork pod (shares the host netns, hop 0) MUST reach IMDS — the positive control
    that pins the block to the hop limit, not to a blanket route/firewall/image problem.

The probe uses the same ECR pull-through busybox the serving/netpol tests use (the
endpoints-only VPC cannot pull curlimages/curl from Docker Hub).
"""

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl
from pytest_jupyter_deploy.kubernetes.namespace import delete_namespace, temporary_namespace

from tests.e2e import _serving_helpers as h

# Throwaway namespace the probes live in. Created without PSA labels so the hostNetwork
# positive-control pod is admitted (mirrors test_network_policy's temp-namespace approach).
PROBE_NS = "e2e-imds"

# IMDSv2 base. With httpTokens=required a token-less GET returns HTTP 401 when the endpoint
# is REACHABLE (proving both reachability AND that IMDSv1 is off); when the hop limit blocks
# it, the connect/read times out instead. That 401-vs-timeout split is how we distinguish
# reachable from blocked — busybox wget exits non-zero for BOTH a 401 and a timeout, so the
# exit code alone can't tell them apart.
IMDS_URL = "http://169.254.169.254/latest/meta-data/"

# Bound the probe: `timeout` hard-caps the exec so a TTL-expired connect can't hang kubectl,
# and wget's own -T caps the connect/read within that.
_WGET_TIMEOUT_S = 5
_HARD_TIMEOUT_S = 15


def _apply_probe(pod: str, image: str, *, host_network: bool) -> None:
    """Apply a probe pod (normal or hostNetwork) and wait for it to be Ready."""
    h.apply_resource(
        "imds-probe.yaml",
        POD_NAME=pod,
        NAMESPACE=PROBE_NS,
        CLIENT_IMAGE=image,
        HOST_NETWORK="true" if host_network else "false",
    )
    run_kubectl("wait", "--for=condition=Ready", f"pod/{pod}", "-n", PROBE_NS, "--timeout=120s", check=True)


def _imds_reachable(pod: str) -> tuple[bool, str]:
    """wget IMDS from the pod; return (reachable, combined output).

    reachable ⇔ the endpoint answered at the HTTP layer (a 401 under IMDSv2, or a 200).
    blocked   ⇔ the connect/read timed out — the response TTL expired before returning.
    Reachability is read from the output, not the exit code, because busybox wget exits
    non-zero for a 401 too.
    """
    command = f"timeout {_HARD_TIMEOUT_S} wget -T {_WGET_TIMEOUT_S} -O - {IMDS_URL} 2>&1"
    res = h.exec_in_pod(PROBE_NS, pod, "sh", "-c", command, check=False)
    out = f"{res.stdout}\n{res.stderr}"
    reachable = res.returncode == 0 or "401" in out
    return reachable, out


@pytest.mark.full_deployment
def test_imds_blocked_from_pod(e2e_deployment: EndToEndDeployment, kubernetes_cluster_login: None) -> None:
    """A normal pod cannot reach IMDS (hop limit 1); a hostNetwork pod can (positive control).

    kubernetes_cluster_login (plugin fixture) does `jd cluster login` once per session.
    """
    e2e_deployment.ensure_deployed()
    image = h.client_image(e2e_deployment)

    # temporary_namespace deletes with --wait=false, so a prior run's namespace may still be
    # Terminating; delete-and-wait first so the create below never hits AlreadyExists.
    delete_namespace(PROBE_NS)
    run_kubectl("wait", "--for=delete", f"namespace/{PROBE_NS}", "--timeout=120s", check=False)

    with temporary_namespace(PROBE_NS):
        # Positive control first: a hostNetwork pod shares the host netns (hop 0) and must
        # reach IMDS — proving the endpoint is up and only the extra hop blocks normal pods.
        _apply_probe("imds-probe-host", image, host_network=True)
        host_reachable, host_out = _imds_reachable("imds-probe-host")
        assert host_reachable, (
            "a hostNetwork pod (hop 0) could NOT reach IMDS — the probe or endpoint itself is "
            f"broken, so the pod-block assertion below would be a false pass. Output:\n{host_out}"
        )

        # The assertion that fails if httpPutResponseHopLimit regressed to 2: a normal pod is
        # one hop beyond the host, so the IMDS response (TTL=1) must expire before reaching it.
        _apply_probe("imds-probe-pod", image, host_network=False)
        pod_reachable, pod_out = _imds_reachable("imds-probe-pod")
        assert not pod_reachable, (
            "a normal pod reached IMDS — httpPutResponseHopLimit is NOT 1 on this node's launch "
            f"path, so pods can read instance metadata / assume the node role via SSRF. Output:\n{pod_out}"
        )
