"""E2E test configuration for the AWS EKS Karpenter template."""

from collections.abc import Generator

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment

from tests.e2e import _serving_helpers as h


def pytest_collection_modifyitems(items: list) -> None:
    """Mark this directory's tests e2e, and collect the GPU-scheduling ones last.

    Grouping every @pytest.mark.gpu test into one trailing block means they share a single
    warm Karpenter GPU node (no drain/re-provision between them), and the GPU-less tests —
    notably test_health, which reads a settled node count — run before any GPU node exists,
    so a mid-join node-exporter DaemonSet can't race their snapshot. Stable partition: order
    within each group is preserved.
    """
    for item in items:
        if "e2e" in str(item.fspath):
            item.add_marker(pytest.mark.e2e)
    items.sort(key=lambda item: item.get_closest_marker("gpu") is not None)


@pytest.fixture(scope="session", autouse=True)
def cleanup_onboarded_qwen_artifacts(e2e_deployment: EndToEndDeployment) -> Generator[None, None, None]:
    """Session teardown: purge the qwen artifacts the serving tests onboard.

    The three qwen serving tests (vllm Path-A, KRO Path-B, KEDA autoscale) all onboard to
    the SAME destinations — weights to models/qwen2.5-7b and the image to the
    workload/vllm/vllm-openai ECR repo — so cleanup is shared once per session rather than
    per-test (a per-test purge would force each to re-onboard from scratch). Weights sit in
    the force_destroy model-store bucket (reaped at `jd down` anyway), but the workload/*
    repo is created imperatively by the onboarder and is NOT in terraform state, so nothing
    else reaps it — this is the only automatic cleanup for it.

    NOTE: interim, not the eventual fix. The symmetric answer is an offboard CodeBuild job
    (inverse of the onboarder) that the tests would dogfood — tracked in issue-3.md.

    Skips entirely when no deployment was used (config-only runs never set _is_deployed), so
    it never calls AWS for a suite that didn't onboard anything."""
    yield
    if not getattr(e2e_deployment, "_is_deployed", False):
        return
    region = h.jd_output(e2e_deployment, "region")
    models_uri = h.jd_output(e2e_deployment, "models_s3_uri")
    h.delete_s3_prefix(f"{models_uri}/qwen2.5-7b")
    # The workload repo is cluster-scoped (<cluster>/workload/...), derived from this
    # deployment's own output — never an account-global 'workload/*' shared with other clusters.
    h.delete_ecr_repo(region, h.workload_image_repo(e2e_deployment))
