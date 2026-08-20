"""Gated live E2E — Path B: KRO graph onboarding, NO Helm (Path B).

Proves the recommended consumer path end-to-end without Helm anywhere:
  1. onboard a bare KRO graph (graph.yaml + field-path sidecar) — the job digest-vendors
     the literal image ref to our ECR and rehosts the literal s3:// weight source into
     our S3, emitting graph-air-gapped.yaml (graph.yaml itself stays pristine);
  2. `kubectl apply` the emitted graph -> registers the RGD + its generated CRD;
  3. `kubectl apply` a ServableGraphInference CR -> KRO expands Deployment + Service ->
     Karpenter provisions a GPU node -> vLLM loads the rehosted weights + serves ->
     invoke /v1/chat/completions;
  4. `kubectl delete` the CR -> KRO CASCADES the children away (the reconcile property
     Helm lacks);
  5. `kubectl delete` the RGD -> the RGD is gone (KRO intentionally leaves the generated
     CRD so existing CRs aren't orphaned — verified live, not asserted).

This is the canonical KRO path: NOTHING uses Helm — the graph is applied straight from
the onboard output. It's the sole test of the KRO reconcile/cascade property (steps 4-5),
which is why the docs steer consumers here rather than to a Helm-wrapped RGD.
Marked `full_deployment` (real GPU + ~15GB weights).
"""

import subprocess
import time

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl

from tests.e2e import _serving_helpers as h

GRAPH_FIXTURE = "qwen"  # tests/e2e/graphs/qwen
GRAPH_NAME = "servable-graph-inference"  # graph.yaml metadata.name -> rehost/out/<name>/
CR_RESOURCE = "servable-graph-inference-cr.yaml"  # in tests/e2e/resources/
CR_NAME = "qwen-graph"  # must match metadata.name in CR_RESOURCE
CHILDREN = ("deployment", "service")


def _child_count() -> int:
    n = 0
    for kind in CHILDREN:
        r = run_kubectl("get", kind, CR_NAME, "-n", h.NAMESPACE, "--ignore-not-found", "-o", "name", check=False)
        if r.stdout.strip():
            n += 1
    return n


@pytest.mark.gpu
@pytest.mark.full_deployment
def test_kro_graph_onboards_and_serves_without_helm(e2e_deployment: EndToEndDeployment) -> None:
    e2e_deployment.ensure_deployed()
    region = h.jd_output(e2e_deployment, "region")
    e2e_deployment.cli.run_command(["jupyter-deploy", "cluster", "login"])

    # 1. Onboard the graph (Path B): image -> ECR by digest, weights -> our S3; the
    #    emitted graph-air-gapped.yaml has our refs baked into the RGD.
    graph = h.onboard_graph(e2e_deployment, region, GRAPH_FIXTURE, GRAPH_NAME)
    text = graph.read_text()
    assert h.WORKLOAD_IMAGE_SUFFIX in text and "@sha256:" in text, (
        "onboard must vendor the image into the graph by digest"
    )
    assert "/models/qwen2.5-7b" in text, "onboard must rewrite the weight source to our models/qwen2.5-7b S3 URI"

    try:
        # 2. Apply the emitted graph (NO Helm) -> RGD + generated CRD.
        subprocess.run(["kubectl", "apply", "-f", str(graph)], check=True, capture_output=True, text=True)
        run_kubectl(
            "wait",
            "--for=jsonpath={.status.state}=Active",
            f"resourcegraphdefinition/{GRAPH_NAME}",
            "--timeout=120s",
            check=True,
        )

        # 3. kubectl-create the CR -> KRO expands children -> Karpenter GPU -> vLLM serves.
        h.apply_resource(CR_RESOURCE)
        rollout = run_kubectl(
            "rollout", "status", f"deployment/{CR_NAME}", "-n", h.NAMESPACE, "--timeout=1200s", check=False
        )
        if rollout.returncode != 0:
            desc = run_kubectl("describe", "pods", "-n", h.NAMESPACE, "-l", f"app={CR_NAME}", check=False).stdout
            raise AssertionError(f"KRO-expanded vLLM did not become ready:\n{rollout.stderr}\n{desc[-2000:]}")
        h.assert_on_karpenter_gpu(CR_NAME)
        assert _child_count() == len(CHILDREN), "KRO must expand all children before invoke"

        h.invoke_chat(e2e_deployment, CR_NAME)

        # 4. kubectl-delete the CR -> KRO cascades the children away.
        run_kubectl("delete", "servablegraphinference", CR_NAME, "-n", h.NAMESPACE, "--timeout=120s", check=True)
        cascaded = False
        for _ in range(24):  # up to ~2 min for the cascade
            if _child_count() == 0:
                cascaded = True
                break
            time.sleep(5)
        assert cascaded, "deleting the CR must cascade-delete its KRO-managed children (Deployment/Service)"
        # 5. Deleting the RGD tears down the operator for this kind. (KRO intentionally
        #    LEAVES the generated CRD in place — deleting the RGD does not remove it, so
        #    existing CRs aren't orphaned — verified live; we don't assert its removal.)
        run_kubectl("delete", "-f", str(graph), "--timeout=120s", check=True)
        rgd = run_kubectl("get", "resourcegraphdefinition", GRAPH_NAME, "--ignore-not-found", "-o", "name", check=False)
        assert not rgd.stdout.strip(), "the RGD itself must be gone after delete"
    finally:
        run_kubectl("delete", "servablegraphinference", CR_NAME, "-n", h.NAMESPACE, "--ignore-not-found", check=False)
        run_kubectl("delete", "-f", str(graph), "--ignore-not-found", check=False)
