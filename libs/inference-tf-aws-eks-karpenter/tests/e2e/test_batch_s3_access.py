"""Live E2E tests for the direct S3 batch storage contract."""

import subprocess
import time
import uuid
from pathlib import Path

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment

from tests.e2e import _serving_helpers as h

POD_NAME = "batch-s3-e2e"


def _aws_cli(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["aws", *args], check=check, capture_output=True, text=True)


def _wait_for_probe(namespace: str) -> str:
    phase = None
    for _ in range(150):
        result = h.kubectl("get", "pod", POD_NAME, "-n", namespace, "-o", "jsonpath={.status.phase}", check=False)
        phase = result.stdout.strip()
        if phase in {"Succeeded", "Failed"}:
            break
        time.sleep(2)

    logs = h.kubectl("logs", POD_NAME, "-n", namespace, check=False).stdout.strip()
    assert phase == "Succeeded", f"The batch S3 probe ended in phase {phase!r}:\n{logs}"
    return logs


def _delete_test_object(bucket: str, key: str) -> None:
    _aws_cli("s3api", "delete-object", "--bucket", bucket, "--key", key, check=False)


@pytest.mark.full_deployment
def test_batch_pod_identity_access(
    e2e_deployment: EndToEndDeployment,
    kubernetes_cluster_login: None,
) -> None:
    """A batch pod can use the exact S3 actions that the platform contract permits."""
    e2e_deployment.ensure_deployed()

    namespace = h.jd_output(e2e_deployment, "workload_namespace")
    service_account = h.jd_output(e2e_deployment, "batch_inference_service_account_name")
    config_map = h.jd_output(e2e_deployment, "batch_storage_config_map_name")
    intake_bucket = h.jd_output(e2e_deployment, "batch_intake_bucket")
    output_bucket = h.jd_output(e2e_deployment, "batch_output_bucket")
    model_store_bucket = h.jd_output(e2e_deployment, "model_store_bucket")
    aws_cli_image = h.jd_output(e2e_deployment, "aws_cli_image_uri")

    run_id = uuid.uuid4().hex
    input_key = f"e2e/{run_id}/input.txt"
    output_key = f"e2e/{run_id}/output.txt"
    model_store_key = f"e2e/{run_id}/model-store.txt"
    payload = Path(f"/tmp/batch-s3-{run_id}.txt")
    payload.write_text("batch S3 E2E payload\n")

    _aws_cli("s3api", "put-object", "--bucket", intake_bucket, "--key", input_key, "--body", str(payload))
    _aws_cli("s3api", "put-object", "--bucket", model_store_bucket, "--key", model_store_key, "--body", str(payload))
    h.kubectl("delete", "pod", POD_NAME, "-n", namespace, "--ignore-not-found", "--wait=false", check=False)

    try:
        h.apply_resource(
            "batch-s3-client.yaml",
            NAMESPACE=namespace,
            SERVICE_ACCOUNT=service_account,
            CONFIG_MAP=config_map,
            AWS_CLI_IMAGE=aws_cli_image,
            INPUT_KEY=input_key,
            OUTPUT_KEY=output_key,
            MODEL_STORE_BUCKET=model_store_bucket,
            MODEL_STORE_KEY=model_store_key,
        )
        logs = _wait_for_probe(namespace)
        assert "batch-s3-e2e: success" in logs
    finally:
        h.kubectl("delete", "pod", POD_NAME, "-n", namespace, "--ignore-not-found", "--wait=false", check=False)
        _delete_test_object(intake_bucket, input_key)
        _delete_test_object(output_bucket, output_key)
        _delete_test_object(model_store_bucket, model_store_key)
        payload.unlink(missing_ok=True)
