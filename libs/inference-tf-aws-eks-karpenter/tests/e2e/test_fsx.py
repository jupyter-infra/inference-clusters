"""Mutating live E2E — FSx for Lustre opt-in path.

Module-scoped fixture flips `enable_fsx=true`, reapplies, yields; reverts on teardown so
subsequent test sessions see the base state. All four tests share one enable+revert
cycle (FSx has an hourly cost floor). Assertions cover the full chain: TF outputs
populated, FS + DRA reach AVAILABLE, CSI driver placed correctly, static PV/PVC bind,
and a consumer pod pinned to the FSx AZ does an RWX round-trip against /models.
"""

import json
import time
from collections.abc import Generator

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl

from tests.e2e import _serving_helpers as h

FSX_NAMESPACE = "kube-system"
# `app=fsx-csi-controller` / `app=fsx-csi-node` are the POD-TEMPLATE labels the chart
# stamps; the parent Deployment/DaemonSet themselves carry only `app.kubernetes.io/*`
# labels. Use the POD label for pod-level assertions and the DaemonSet's own NAME for
# the daemonset-level query.
FSX_CSI_CONTROLLER_LABEL = "app=fsx-csi-controller"
FSX_CSI_NODE_DS_NAME = "fsx-csi-node"
FSX_CONSUMER_POD = "fsx-consumer-e2e"


@pytest.fixture(scope="module")
def fsx_enabled(e2e_deployment: EndToEndDeployment) -> Generator[EndToEndDeployment, None, None]:
    """Enable FSx once for the module, then revert to base state at teardown.

    Both the enable and the revert are full reconfigure+reapply passes and each takes
    non-trivial minutes; wrapping them in a module-scoped fixture runs them exactly twice
    regardless of how many tests consume it. The revert is in `finally` so a mid-test
    failure still tears the file system down."""
    e2e_deployment.ensure_deployed()
    e2e_deployment.update_override_value("enable_fsx", True)
    # FSx PERSISTENT_2 file-system creation is slow (~10-20 min) and the DRA add another
    # few — give the apply a generous ceiling.
    e2e_deployment.ensure_deployed_with([], timeout_seconds=2400)
    try:
        yield e2e_deployment
    finally:
        e2e_deployment.update_override_value("enable_fsx", False)
        e2e_deployment.ensure_deployed_with([], timeout_seconds=2400)


def _await_fsx_available(region: str, fs_id: str, timeout_s: int = 1800) -> None:
    """Poll DescribeFileSystems until Lifecycle=AVAILABLE; defence-in-depth for a
    mid-run describe race, fail loud on terminal Lifecycles."""
    deadline = time.time() + timeout_s
    lifecycle = "UNKNOWN"
    while time.time() < deadline:
        systems = h.fsx_client(region).describe_file_systems(FileSystemIds=[fs_id])["FileSystems"]
        assert systems, f"DescribeFileSystems returned no results for {fs_id}"
        lifecycle = systems[0].get("Lifecycle", "UNKNOWN")
        if lifecycle == "AVAILABLE":
            return
        if lifecycle in ("FAILED", "DELETING", "MISCONFIGURED"):
            raise AssertionError(f"FSx file system {fs_id} entered terminal Lifecycle={lifecycle}")
        time.sleep(15)
    raise AssertionError(f"FSx file system {fs_id} never reached AVAILABLE (last Lifecycle={lifecycle})")


def _await_dra_available(region: str, fs_id: str, timeout_s: int = 900) -> dict:
    """Poll DescribeDataRepositoryAssociations until Lifecycle=AVAILABLE; return the assoc."""
    deadline = time.time() + timeout_s
    lifecycle = "UNKNOWN"
    while time.time() < deadline:
        associations = h.fsx_client(region).describe_data_repository_associations(
            Filters=[{"Name": "file-system-id", "Values": [fs_id]}],
        )["Associations"]
        if associations:
            association = dict(associations[0])
            lifecycle = str(association.get("Lifecycle", "UNKNOWN"))
            if lifecycle == "AVAILABLE":
                return association
            if lifecycle in ("FAILED", "DELETING", "MISCONFIGURED"):
                raise AssertionError(f"DRA on {fs_id} entered terminal Lifecycle={lifecycle}")
        time.sleep(10)
    raise AssertionError(f"DRA on {fs_id} never reached AVAILABLE (last Lifecycle={lifecycle})")


@pytest.mark.mutating
def test_fsx_outputs_and_control_plane(fsx_enabled: EndToEndDeployment) -> None:
    """Terraform outputs flip populated when FSx is on, and the FS + DRA reach AVAILABLE.

    This is the control-plane half of the mutating cycle: it never contacts the cluster.
    A break here (empty outputs, stuck lifecycle, DRA pointing at the wrong prefix) means
    the Terraform wiring in platform_fsx.tf regressed."""
    # Terraform's tostring(true) yields the lowercase "true" (not Python's "True").
    assert h.jd_output(fsx_enabled, "fsx_enabled") == "true", (
        "fsx_enabled output must be 'true' after flipping enable_fsx=true"
    )
    fs_id = h.jd_output(fsx_enabled, "fsx_file_system_id")
    dns = h.jd_output(fsx_enabled, "fsx_dns_name")
    mount_name = h.jd_output(fsx_enabled, "fsx_mount_name")
    az = h.jd_output(fsx_enabled, "fsx_availability_zone")
    dra_path = h.jd_output(fsx_enabled, "fsx_data_repository_path")
    model_store = h.jd_output(fsx_enabled, "model_store_bucket")
    region = h.jd_output(fsx_enabled, "region")

    assert fs_id.startswith("fs-"), f"expected fsx_file_system_id to look like fs-<id>, got {fs_id!r}"
    assert dns.endswith("amazonaws.com"), f"expected FSx DNS to be an AWS hostname, got {dns!r}"
    assert mount_name, "fsx_mount_name output must be non-empty"
    # AZ is <region><letter>, e.g. us-west-2a — must be in the deployment region.
    assert az.startswith(region), f"FSx AZ ({az}) must be inside the deployment region ({region})"
    # DRA points at s3://<model_store>/models/ (trailing slash matters).
    assert dra_path == f"s3://{model_store}/models/", (
        f"DRA path must point at s3://<model_store>/models/, got {dra_path!r}"
    )

    _await_fsx_available(region, fs_id)
    dra = _await_dra_available(region, fs_id)
    assert dra.get("DataRepositoryPath") == f"s3://{model_store}/models/", dra
    # DRA maps Lustre root to the S3 `models/` prefix. The FSx CSI PV mounts
    # Lustre root at the pod's mountpoint (typically /models), so an S3 object
    # `models/foo.bin` appears at pod path `/models/foo.bin` — same layout as
    # the S3-mount PV. Any other value (notably `/models`) double-nests the
    # imported content and silently no-ops hydration.
    assert dra.get("FileSystemPath") == "/", (
        f"DRA FileSystemPath must be / (so Lustre root maps directly to the "
        f"S3 models/ prefix), got {dra.get('FileSystemPath')!r}"
    )


@pytest.mark.mutating
def test_fsx_csi_driver_installed_and_placed(
    fsx_enabled: EndToEndDeployment,
    kubernetes_cluster_login: None,
) -> None:
    """The aws-fsx-csi-driver Helm release is up: controller on system MNG, node-plugin DaemonSet ready.

    The controller Deployment is a control-loop pod → must land on the tainted system MNG.
    The node-plugin is a DaemonSet that tolerates all taints so a Karpenter GPU/CPU node
    can mount FSx PVCs; it must have desired == ready across every current node."""
    h.assert_pods_by_selector_on_system_mng(FSX_NAMESPACE, FSX_CSI_CONTROLLER_LABEL, "aws-fsx-csi-driver controller")

    # DaemonSet lookup by NAME (the DS's own metadata labels don't include the
    # pod-template `app=fsx-csi-node` label — a -l selector on it returns zero items).
    node_ds = run_kubectl(
        "get",
        "daemonset",
        FSX_CSI_NODE_DS_NAME,
        "-n",
        FSX_NAMESPACE,
        "-o",
        "jsonpath={.status.desiredNumberScheduled},{.status.numberReady}",
        check=True,
    ).stdout.strip()
    desired, _, ready = node_ds.partition(",")
    assert desired and desired == ready, (
        f"aws-fsx-csi-driver node DaemonSet not fully ready (desired={desired!r}, ready={ready!r})"
    )
    assert int(desired) >= 1, "expected at least one FSx CSI node-plugin pod scheduled"


@pytest.mark.mutating
def test_fsx_pv_pvc_bound_and_wired(
    fsx_enabled: EndToEndDeployment,
    kubernetes_cluster_login: None,
) -> None:
    """The storage chart's FSx PV + PVC render, bind, and carry the correct volumeHandle.

    Guards the chart-side wiring: the mountname/dnsname/fs-id from Terraform must flow
    through platform_storage.tf → the storage helm_release → the fsx-mount.yaml template
    and back out as a bound PV that the FSx CSI driver would actually mount.
    """
    workload_ns = h.jd_output(fsx_enabled, "workload_namespace")
    fs_id = h.jd_output(fsx_enabled, "fsx_file_system_id")
    mount_name = h.jd_output(fsx_enabled, "fsx_mount_name")
    dns = h.jd_output(fsx_enabled, "fsx_dns_name")

    # PVC name is baked into the chart values (fsx.claimName), not currently exposed as a
    # Terraform output. The value is stable ("model-store-fsx") and matches values.yaml.
    pvc_name = "model-store-fsx"

    pvc_phase = run_kubectl(
        "get",
        "pvc",
        pvc_name,
        "-n",
        workload_ns,
        "-o",
        "jsonpath={.status.phase}",
        check=True,
    ).stdout.strip()
    assert pvc_phase == "Bound", f"expected PVC {workload_ns}/{pvc_name} to be Bound, got {pvc_phase!r}"

    pv = json.loads(
        run_kubectl(
            "get",
            "pv",
            pvc_name,
            "-o",
            "json",
            check=True,
        ).stdout
    )
    csi = pv["spec"].get("csi", {})
    assert csi.get("driver") == "fsx.csi.aws.com", f"PV CSI driver must be fsx.csi.aws.com, got {csi}"
    assert csi.get("volumeHandle") == f"{fs_id}::{mount_name}", (
        f"PV volumeHandle must be <fs-id>::<mount-name>, got {csi.get('volumeHandle')!r}"
    )
    assert csi.get("volumeAttributes", {}).get("dnsname") == dns
    assert csi.get("volumeAttributes", {}).get("mountname") == mount_name
    assert "flock" in pv["spec"].get("mountOptions", []), (
        "FSx PV must mount with flock (POSIX file locks — SafeTensors mmap, sqlite, torch)"
    )
    access_modes = pv["spec"].get("accessModes", [])
    assert access_modes == ["ReadWriteMany"], f"FSx PV must be RWX, got accessModes={access_modes}"


@pytest.mark.mutating
def test_fsx_consumer_pod_mounts_and_readwrites(
    fsx_enabled: EndToEndDeployment,
    kubernetes_cluster_login: None,
) -> None:
    """A pod on a Karpenter node in the FSx AZ mounts /models RWX, writes, reads, exits 0.

    End-to-end proof of the data plane: the SG rules allow Lustre RPC through, the CSI
    driver hands the mount to the pod, `flock` mount options are honored, and the PVC's
    Lustre backing accepts a POSIX write. Pod is pinned to the FSx AZ (single-AZ FS).
    """
    workload_ns = h.jd_output(fsx_enabled, "workload_namespace")
    zone = h.jd_output(fsx_enabled, "fsx_availability_zone")
    image = h.client_image(fsx_enabled)

    run_kubectl(
        "delete",
        "pod",
        FSX_CONSUMER_POD,
        "-n",
        workload_ns,
        "--ignore-not-found",
        "--wait=false",
        check=False,
    )
    try:
        h.apply_resource(
            "fsx-consumer.yaml",
            image=image,
            namespace=workload_ns,
            claim_name="model-store-fsx",
            zone=zone,
        )
        # First-time mount can be slow: Karpenter must provision a CPU node in the FSx AZ
        # AND the FSx CSI node plugin must attach the Lustre client — budget 10 min.
        run_kubectl(
            "wait",
            "--for=jsonpath={.status.phase}=Succeeded",
            f"pod/{FSX_CONSUMER_POD}",
            "-n",
            workload_ns,
            "--timeout=600s",
            check=True,
        )
        logs = run_kubectl("logs", FSX_CONSUMER_POD, "-n", workload_ns, check=True).stdout
        assert "[fsx-consumer] OK" in logs, (
            f"consumer pod completed but did not print the OK sentinel; logs tail:\n{logs[-2000:]}"
        )
        # Confirm the pod actually ran in the FSx AZ (defence against a stale/no-op
        # affinity — a broken selector would fail earlier as Unschedulable, but a matching
        # label on the wrong zone value would silently pass).
        node = run_kubectl(
            "get",
            "pod",
            FSX_CONSUMER_POD,
            "-n",
            workload_ns,
            "-o",
            "jsonpath={.spec.nodeName}",
            check=True,
        ).stdout.strip()
        assert node, "consumer pod has no nodeName — scheduling never completed"
        node_zone = run_kubectl(
            "get",
            "node",
            node,
            "-o",
            r"jsonpath={.metadata.labels.topology\.kubernetes\.io/zone}",
            check=True,
        ).stdout.strip()
        assert node_zone == zone, (
            f"consumer pod landed in {node_zone!r} but the FSx file system is in {zone!r} "
            "— every mount would pay inter-AZ transfer; check the nodeAffinity block"
        )
    finally:
        run_kubectl(
            "delete",
            "pod",
            FSX_CONSUMER_POD,
            "-n",
            workload_ns,
            "--ignore-not-found",
            "--wait=false",
            check=False,
        )
