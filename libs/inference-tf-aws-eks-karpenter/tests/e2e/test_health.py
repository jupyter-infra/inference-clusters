"""E2E tests for the `jd health` command on the EKS Karpenter template.

Modeled on the eks-oidc health e2e, adapted to this template's air-gapped shape:
  - the cluster / components / images layers apply;
  - the load-balancer and connection layers self-skip (endpoints-only VPC, no public
    ingress/ELB, no open_url) — these tests ASSERT that skip, so a future regression that
    accidentally wires an LB/connection check is caught.

Marked `full_deployment`: they read a live deployment (fast, no GPU) but require the
cluster to exist, so they run in the full-deploy path (or against an existing project
with `--with-full-deployment`).
"""

import json
import time

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment

# Layers that must report a row (cluster + the two that self-skip). Components/images are
# variable-length, asserted separately against the manifest.
_STATUS_CATEGORIES = ("healthy", "in-progress", "degraded")


@pytest.mark.full_deployment
def test_health_all_layers(e2e_deployment: EndToEndDeployment) -> None:
    """`jd health` runs every layer and prints a row for each (human-readable output)."""
    e2e_deployment.ensure_deployed()

    output = e2e_deployment.cli.run_command(["jupyter-deploy", "health"]).stdout

    for layer in ["cluster", "load-balancer", "components", "images"]:
        assert layer in output, f"expected layer '{layer}' in health output"


@pytest.mark.full_deployment
def test_health_json_shape(e2e_deployment: EndToEndDeployment) -> None:
    """--json returns the documented shape: a layers list + a connection object."""
    e2e_deployment.ensure_deployed()

    data = json.loads(e2e_deployment.cli.run_command(["jupyter-deploy", "health", "--json"]).stdout)

    assert "layers" in data, f"expected 'layers' key, got: {list(data.keys())}"
    assert "connection" in data, f"expected 'connection' key, got: {list(data.keys())}"

    for entry in data["layers"]:
        for field in ("layer", "name", "status", "status_category", "detail", "sub_component", "skipped"):
            assert field in entry, f"layer row missing '{field}': {entry}"
        assert entry["status_category"] in _STATUS_CATEGORIES

    conn = data["connection"]
    for field in ("status_category", "detail", "skipped"):
        assert field in conn, f"connection missing '{field}': {conn}"


@pytest.mark.full_deployment
def test_health_cluster_layer(e2e_deployment: EndToEndDeployment) -> None:
    """--cluster reports a single healthy Active row with a version detail."""
    e2e_deployment.ensure_deployed()

    data = json.loads(e2e_deployment.cli.run_command(["jupyter-deploy", "health", "--cluster", "--json"]).stdout)

    layers = data["layers"]
    assert len(layers) == 1
    assert layers[0]["layer"] == "cluster"
    assert layers[0]["status_category"] == "healthy"
    assert layers[0]["status"] == "Active"
    assert layers[0]["detail"].startswith("v"), f"cluster detail should be a version, got {layers[0]['detail']!r}"
    assert layers[0]["skipped"] is False


# GPU-only DaemonSets: on the GPU-less full_deployment cluster they have no node to run on
# (desired pods = 0), so their DaemonSet status reads Degraded — expected, not a failure.
# Their `-chart` HelmRelease twins carry the authoritative install-state signal and MUST be
# healthy. See the manifest components: block + AGENT.md for the rationale.
_GPU_DAEMONSET_COMPONENTS = {"dcgm-exporter", "nvidia-device-plugin"}


def _components_layers(e2e_deployment: EndToEndDeployment) -> list[dict]:
    """One `jd health --components --json` snapshot of the component rows."""
    data = json.loads(e2e_deployment.cli.run_command(["jupyter-deploy", "health", "--components", "--json"]).stdout)
    layers: list[dict] = data["layers"]
    return layers


@pytest.mark.full_deployment
def test_health_components_layer(e2e_deployment: EndToEndDeployment) -> None:
    """--components returns one row per manifest component; all healthy EXCEPT the GPU-only
    DaemonSets, which read Degraded on a GPU-less cluster (their HelmRelease twins stay healthy).

    Poll-until-converged: the per-node node-exporter DaemonSet reads Degraded for a beat when
    a Karpenter node has just joined (desired count bumped, new pod still pulling its image /
    not yet Ready) — `jd health` is a single-shot snapshot, so a bare read can catch that
    window. We re-read until every must-be-healthy component converges; a genuinely broken
    component still fails once the budget elapses. Mirrors the poll loops used across this
    suite (see test_keda_scale_from_zero, _serving_helpers)."""
    e2e_deployment.ensure_deployed()

    manifest_components = e2e_deployment.get_manifest().get_components()

    # DaemonSets on a node that just joined (or, for the GPU-only ones, that has no node at all)
    # converge on their own schedule; a node-exporter pod on a freshly-joined node reads
    # in-progress until its image pulls, so budget over a cold image-pull + node-join before we
    # trust a not-healthy read.
    interval_s = 10
    deadline = time.monotonic() + 300
    layers = _components_layers(e2e_deployment)
    while True:
        must_be_healthy = [
            e for e in layers if e["name"] not in _GPU_DAEMONSET_COMPONENTS and not e["name"].endswith("-chart")
        ]
        # -chart HelmRelease twins are pod-count-agnostic and must be healthy too, but they are
        # not subject to the node-join race, so they gate on the same converged snapshot below.
        if all(e["status_category"] == "healthy" for e in must_be_healthy) or time.monotonic() >= deadline:
            break
        time.sleep(interval_s)
        layers = _components_layers(e2e_deployment)

    assert len(layers) == len(manifest_components), (
        f"expected {len(manifest_components)} component rows, got {len(layers)}"
    )
    names = {entry["name"] for entry in layers}
    for name in manifest_components:
        assert name in names, f"expected component '{name}' in health output"

    for entry in layers:
        assert entry["layer"] == "components"
        assert entry["status"] != ""
        if entry["name"] in _GPU_DAEMONSET_COMPONENTS:
            # No GPU node up -> desired=0 -> Degraded. Accept degraded (or healthy if a GPU
            # node happens to exist); the HelmRelease twin is asserted healthy below.
            assert entry["status_category"] in ("degraded", "healthy"), (
                f"GPU DaemonSet '{entry['name']}' unexpected status: {entry['status_category']}"
            )
        else:
            assert entry["status_category"] == "healthy", (
                f"component '{entry['name']}' not healthy: {entry['status_category']} ({entry['detail']})"
            )

    # The HelmRelease twins of the GPU DaemonSets must be healthy regardless of GPU nodes.
    by_name = {entry["name"]: entry for entry in layers}
    for daemonset in _GPU_DAEMONSET_COMPONENTS:
        chart = f"{daemonset}-chart"
        assert by_name[chart]["status_category"] == "healthy", (
            f"HelmRelease '{chart}' must be healthy (chart install-state), got {by_name[chart]['status_category']}"
        )


@pytest.mark.full_deployment
def test_health_images_layer(e2e_deployment: EndToEndDeployment) -> None:
    """--images returns one Available row per manifest image at its vendored tag."""
    e2e_deployment.ensure_deployed()

    manifest_images = e2e_deployment.get_manifest().get_images()

    data = json.loads(e2e_deployment.cli.run_command(["jupyter-deploy", "health", "--images", "--json"]).stdout)
    layers = data["layers"]

    assert len(layers) == len(manifest_images), f"expected {len(manifest_images)} image rows, got {len(layers)}"
    names = {entry["name"] for entry in layers}
    for name in manifest_images:
        assert name in names, f"expected image '{name}' in health output"

    for entry in layers:
        assert entry["layer"] == "images"
        # Status reflects ECR presence; every vendored platform image must be present.
        assert entry["status"] == "Available", f"image '{entry['name']}' not Available: {entry['status']}"
        assert entry["status_category"] == "healthy"
        assert entry["detail"] == "vendored", f"image '{entry['name']}' should be at the vendored tag"


@pytest.mark.full_deployment
def test_health_load_balancer_and_connection_skip(e2e_deployment: EndToEndDeployment) -> None:
    """The LB + connection layers MUST self-skip on this air-gapped template (no ingress/ELB,
    no open_url) — asserting the skip guards against accidentally wiring an external check."""
    e2e_deployment.ensure_deployed()

    lb = json.loads(e2e_deployment.cli.run_command(["jupyter-deploy", "health", "--load-balancer", "--json"]).stdout)
    assert len(lb["layers"]) == 1
    assert lb["layers"][0]["layer"] == "load-balancer"
    assert lb["layers"][0]["skipped"] is True, "load-balancer layer must self-skip (no ELB on this template)"

    conn = json.loads(e2e_deployment.cli.run_command(["jupyter-deploy", "health", "--connection", "--json"]).stdout)
    assert conn["layers"] == [], "expected empty 'layers' with --connection only"
    assert conn["connection"]["skipped"] is True, "connection layer must self-skip (no open_url on this template)"
