"""Gated live serving E2E — the full inference path on a real GPU node.

Onboards a realistic consumer chart (vLLM serving Qwen2.5-7B-Instruct), installs it, and
proves the whole chain the POC measures:
  1. onboarder digest-vendors the vLLM image to ECR + rehosts the model weights
     (SageMaker JumpStart, same-region s3://) into our S3;
  2. `helm install` with the emitted overrides -> the GPU pod pends -> Karpenter
     provisions a g6 node -> weights mount read-only from S3 -> vLLM serves;
  3. a client pod POSTs /v1/chat/completions and gets a valid completion back.

Marked `full_deployment` (real GPU time + ~15GB weight copy) — runs only against a
deployed cluster with full-deploy=true. The chart is a fixture, never in the template.
"""

import subprocess

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl

from tests.e2e import _serving_helpers as h

CHART = h.CHARTS_DIR / "vllm-qwen"
RELEASE = "vllm-qwen-e2e"


@pytest.mark.gpu
@pytest.mark.full_deployment
def test_vllm_qwen_serves_on_karpenter_gpu(e2e_deployment: EndToEndDeployment) -> None:
    e2e_deployment.ensure_deployed()
    region = h.jd_output(e2e_deployment, "region")
    e2e_deployment.cli.run_command(["jupyter-deploy", "cluster", "login"])

    # 1. Onboard the chart (image -> ECR, weights -> our S3), get overrides.yaml.
    overrides = h.onboard_chart(e2e_deployment, region, "vllm-qwen")
    assert h.WORKLOAD_IMAGE_SUFFIX in overrides.read_text(), "onboard must vendor the vLLM image to workload/*"

    try:
        # 2. Install -> pod pends -> Karpenter provisions a GPU node -> vLLM loads + serves.
        subprocess.run(
            ["helm", "install", RELEASE, str(CHART), "-n", h.NAMESPACE, "-f", str(overrides)],
            check=True,
            capture_output=True,
            text=True,
        )
        # Rollout can take ~10-15 min (node provision + ~15GB weight read + CUDA warmup).
        rollout = run_kubectl(
            "rollout", "status", f"deployment/{RELEASE}", "-n", h.NAMESPACE, "--timeout=1200s", check=False
        )
        if rollout.returncode != 0:
            pods = run_kubectl(
                "get", "pods", "-n", h.NAMESPACE, "-l", f"app={RELEASE}", "-o", "wide", check=False
            ).stdout
            desc = run_kubectl("describe", "pods", "-n", h.NAMESPACE, "-l", f"app={RELEASE}", check=False).stdout
            raise AssertionError(f"vLLM rollout failed:\n{rollout.stderr}\n{pods}\n{desc[-2000:]}")

        # The pod must have landed on a Karpenter GPU node.
        h.assert_on_karpenter_gpu(RELEASE)

        # 3. Invoke: POST /v1/chat/completions through the ClusterIP Service.
        h.invoke_chat(e2e_deployment, RELEASE)
    finally:
        subprocess.run(["helm", "uninstall", RELEASE, "-n", h.NAMESPACE], check=False, capture_output=True)
