"""Gated live E2E — Path B `builds:`: the onboarder BUILDS an image with no upstream.

Exercises the builds: sidecar contract for an image that has NO published upstream to
skopeo-copy (built from source). The onboarder:
  1. tars the build context (build-ctx/Dockerfile) shipped in the graph artifact,
  2. fires the image-build CodeBuild job (SOURCE_REF/IMAGE_NAME/IMAGE_TAG),
  3. resolves the pushed image's digest from our ECR,
  4. rewrites the graph field-path to <ecr>/workload/<name>@<digest> — the same immutable
     contract as a vendored image, covered by the same field-level backstop.

Complements test_image_build.py (which exercises the image-build job STANDALONE): this
proves the ONBOARDER orchestrates that job from a graph's builds: block end to end.

Marked full_deployment: needs a deployed cluster (ECR + onboarder + image-build jobs).
Runs only with full-deploy=true.
"""

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment

from tests.e2e import _serving_helpers as h

GRAPH_FIXTURE = "imgbuild-graph"  # tests/e2e/graphs/imgbuild-graph
GRAPH_NAME = "imgbuild-graph"  # graph.yaml metadata.name -> rehost/out/<name>/
IMAGE_NAME = "imgbuild-graph-app"  # builds[].name -> <ecr>/workload/imgbuild-graph-app
IMAGE_TAG = "v1"  # builds[].tag


@pytest.mark.full_deployment
def test_onboarder_builds_image_and_rewrites_graph(e2e_deployment: EndToEndDeployment) -> None:
    e2e_deployment.ensure_deployed()
    region = h.jd_output(e2e_deployment, "region")
    registry = h.jd_output(e2e_deployment, "ecr_registry")
    workload_prefix = h.jd_output(e2e_deployment, "workload_repo_prefix")
    repository = f"{workload_prefix}/{IMAGE_NAME}"

    try:
        # Onboard the graph: the builds: entry makes the onboarder BUILD build-ctx/ into
        # our ECR and bake the digest into the emitted graph (graph.yaml stays pristine).
        graph = h.onboard_graph(e2e_deployment, region, GRAPH_FIXTURE, GRAPH_NAME)
        text = graph.read_text()

        # The built image (no upstream — built from the shipped context) is in our ECR.
        assert h.ecr_image_exists(region, repository, IMAGE_TAG), (
            f"expected {repository}:{IMAGE_TAG} in ECR after the onboarder's builds: orchestration"
        )
        # The graph's build field-path was rewritten to our ECR by DIGEST (not the literal).
        assert f"{registry}/{repository}@sha256:" in text, (
            f"onboarder must rewrite the builds: path to our ECR @digest; emitted graph:\n{text}"
        )
        assert "imgbuild-graph-app:v1" not in text, "the literal placeholder ref must not survive onboarding"
    finally:
        # Always purge the throwaway repo — pass or fail — so it never lingers.
        h.delete_ecr_repo(region, repository)
