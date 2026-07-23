"""Gated live E2E — the non-mutating component/image operational verbs.

The manifest wires four verb families beyond `status` (show/logs/restart/reconcile on
components, show on images — ported to parity with eks-oidc). `status` is exercised by
test_health.py; the unit test proves each verb is DECLARED with the right method + a
backing cmd block. This test proves the READ verbs actually EXECUTE against a live cluster
— the cmd-block wiring (source-keys, the expected_cluster_config/cluster_arn guard, the
handler parsing the result) only fails live, which raw-dict/declaration checks can't catch.

Scope: read verbs only, across representative component types —
  - `jd component show`  on a Deployment (karpenter) AND a HelmRelease (dcgm-exporter-chart)
  - `jd component logs`  on a Deployment (karpenter)
  - `jd image show`      on a vendored image (grafana)
Mutating verbs (restart, reconcile) are deliberately NOT tested: restart rolling-restarts a
live platform operator and reconcile needs a drift scenario — both perturb a shared cluster
for little marginal coverage over the identical read-verb wiring proved here.

Marked `full_deployment`: pure reads against the deployed cluster (fast, no GPU, no mutation).
"""

import json

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment


@pytest.mark.full_deployment
def test_component_show_deployment(e2e_deployment: EndToEndDeployment) -> None:
    """`jd component show` returns the full Deployment resource JSON for a Deployment component."""
    e2e_deployment.ensure_deployed()

    data = json.loads(
        e2e_deployment.cli.run_command(["jupyter-deploy", "component", "show", "--name", "karpenter", "--json"]).stdout
    )
    assert data["name"] == "karpenter"
    assert data["resource"]["kind"] == "Deployment", (
        f"expected a Deployment resource, got {data['resource'].get('kind')!r}"
    )


@pytest.mark.full_deployment
def test_component_show_helmrelease(e2e_deployment: EndToEndDeployment) -> None:
    """`jd component show` returns the release info for a HelmRelease component (helm.show wiring)."""
    e2e_deployment.ensure_deployed()

    data = json.loads(
        e2e_deployment.cli.run_command(
            ["jupyter-deploy", "component", "show", "--name", "dcgm-exporter-chart", "--json"]
        ).stdout
    )
    # resource-name maps the -chart component to the underlying release name.
    assert data["name"] == "dcgm-exporter"
    assert data["resource"], "expected a non-empty helm release resource"


@pytest.mark.full_deployment
def test_component_logs_deployment(e2e_deployment: EndToEndDeployment) -> None:
    """`jd component logs` streams a Deployment component's pod logs (the `--` passthrough +
    the expected_cluster_config/cluster_arn guard)."""
    e2e_deployment.ensure_deployed()

    logs = e2e_deployment.cli.run_command(
        ["jupyter-deploy", "component", "logs", "--name", "karpenter", "--", "--tail", "5"]
    ).stdout
    assert logs.strip(), "expected non-empty Karpenter logs"


@pytest.mark.full_deployment
def test_image_show(e2e_deployment: EndToEndDeployment) -> None:
    """`jd image show` resolves a vendored image's ECR repo URI + scanner backend."""
    e2e_deployment.ensure_deployed()

    data = json.loads(
        e2e_deployment.cli.run_command(["jupyter-deploy", "image", "show", "--name", "grafana", "--json"]).stdout
    )
    assert data["name"] == "grafana"
    assert ".dkr.ecr." in data["repository_uri"], f"expected an ECR repo URI, got {data['repository_uri']!r}"
