"""Gated live E2E — the image-build job: build a source dir into the cluster's ECR.

Exercises the full image-build contract a consumer follows for an image that has no
published upstream to import:
  1. tar a source dir (Dockerfile + build context, here incl. a custom .whl)
  2. upload it to the image-build input S3 prefix
  3. `aws codebuild start-build` with SOURCE_REF + IMAGE_NAME + IMAGE_TAG
  4. the job pulls the tarball, `docker build`s, and pushes to <ecr>/workload/<name>:<tag>

The fixture (sources/imgbuild-min) is deliberately minimal but installs a custom wheel
from the build context — proving arbitrary source dirs (incl. .whl files) build+publish,
which the base64-env approach could not carry. This test doubles as the worked example
of how a workloads repo uses the image-build primitive.

Marked `full_deployment`: needs a deployed cluster (ECR + CodeBuild). Runs only with
full-deploy=true.
"""

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment

from tests.e2e import _serving_helpers as h

SOURCE = "imgbuild-min"  # sources/<SOURCE> fixture dir
IMAGE_NAME = "imgbuild-e2e"  # -> <ecr>/workload/imgbuild-e2e
IMAGE_TAG = "v1"
# ~10 min ceiling (30 x 20s): a slim base + one wheel builds in ~1-2 min; headroom for
# CodeBuild cold start.
MAX_POLLS = 30


@pytest.mark.full_deployment
def test_image_build_publishes_source_dir_to_ecr(e2e_deployment: EndToEndDeployment) -> None:
    e2e_deployment.ensure_deployed()
    region = h.jd_output(e2e_deployment, "region")
    workload_prefix = h.jd_output(e2e_deployment, "workload_repo_prefix")
    repository = f"{workload_prefix}/{IMAGE_NAME}"

    try:
        # Build the source dir -> workload ECR (uploads tarball, starts build, polls).
        image_ref = h.build_image(e2e_deployment, region, SOURCE, IMAGE_NAME, IMAGE_TAG, max_polls=MAX_POLLS)

        # The built image (with the custom wheel installed at build time) is in our ECR.
        assert h.ecr_image_exists(region, repository, IMAGE_TAG), (
            f"expected {repository}:{IMAGE_TAG} in ECR after image-build; ref was {image_ref}"
        )
    finally:
        # Always purge the throwaway repo — pass or fail — so it never lingers.
        h.delete_ecr_repo(region, repository)
