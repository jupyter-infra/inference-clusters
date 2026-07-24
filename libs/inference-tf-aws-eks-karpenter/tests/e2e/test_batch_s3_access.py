"""Live E2E tests for the direct S3 batch storage contract."""

import json
import uuid

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment

from tests.e2e import _serving_helpers as h

POD_NAME = "batch-s3-e2e"
PAYLOAD = "batch S3 E2E payload\n"


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
    denied_intake_key = f"{input_key}-denied"
    denied_model_key = f"{model_store_key}-denied"

    h.s3_put_object(intake_bucket, input_key, PAYLOAD.encode())
    h.s3_put_object(model_store_bucket, model_store_key, PAYLOAD.encode())
    h.run_kubectl("delete", "pod", POD_NAME, "-n", namespace, "--ignore-not-found", "--wait=false", check=False)

    try:
        h.apply_resource(
            "batch-s3-client.yaml",
            NAMESPACE=namespace,
            SERVICE_ACCOUNT=service_account,
            CONFIG_MAP=config_map,
            AWS_CLI_IMAGE=aws_cli_image,
        )
        h.run_kubectl("wait", "--for=condition=Ready", f"pod/{POD_NAME}", "-n", namespace, "--timeout=300s")

        pod = h.run_kubectl("get", "pod", POD_NAME, "-n", namespace, "-o", "json").stdout
        pod_spec = json.loads(pod)["spec"]
        assert pod_spec["serviceAccountName"] == service_account
        assert pod_spec["containers"][0]["envFrom"] == [{"configMapRef": {"name": config_map}}]

        pod_intake_bucket = h.exec_in_pod(namespace, POD_NAME, "printenv", "BATCH_INTAKE_BUCKET").stdout.strip()
        pod_output_bucket = h.exec_in_pod(namespace, POD_NAME, "printenv", "BATCH_OUTPUT_BUCKET").stdout.strip()
        assert pod_intake_bucket == intake_bucket
        assert pod_output_bucket == output_bucket

        identity = h.exec_in_pod(
            namespace, POD_NAME, "aws", "sts", "get-caller-identity", "--query", "Arn", "--output", "text"
        ).stdout.strip()
        assert "batch-inference" in identity

        listed_input = h.exec_in_pod(
            namespace,
            POD_NAME,
            "aws",
            "s3api",
            "list-objects-v2",
            "--bucket",
            intake_bucket,
            "--prefix",
            input_key,
            "--query",
            f"Contents[?Key=='{input_key}'].Key",
            "--output",
            "text",
        ).stdout.strip()
        assert listed_input == input_key
        h.exec_in_pod(
            namespace,
            POD_NAME,
            "aws",
            "s3api",
            "get-object",
            "--bucket",
            intake_bucket,
            "--key",
            input_key,
            "/tmp/input",
        )
        h.exec_in_pod(
            namespace,
            POD_NAME,
            "aws",
            "s3api",
            "put-object",
            "--bucket",
            output_bucket,
            "--key",
            output_key,
            "--body",
            "/tmp/input",
        )
        h.exec_in_pod(
            namespace,
            POD_NAME,
            "aws",
            "s3api",
            "get-object",
            "--bucket",
            output_bucket,
            "--key",
            output_key,
            "/tmp/output",
        )
        output_payload = h.exec_in_pod(namespace, POD_NAME, "cat", "/tmp/output").stdout
        assert output_payload == PAYLOAD
        listed_output = h.exec_in_pod(
            namespace,
            POD_NAME,
            "aws",
            "s3api",
            "list-objects-v2",
            "--bucket",
            output_bucket,
            "--prefix",
            output_key,
            "--query",
            f"Contents[?Key=='{output_key}'].Key",
            "--output",
            "text",
        ).stdout.strip()
        assert listed_output == output_key

        denied_commands = (
            (
                "aws",
                "s3api",
                "put-object",
                "--bucket",
                intake_bucket,
                "--key",
                denied_intake_key,
                "--body",
                "/tmp/input",
            ),
            ("aws", "s3api", "delete-object", "--bucket", intake_bucket, "--key", input_key),
            ("aws", "s3api", "delete-object", "--bucket", output_bucket, "--key", output_key),
            (
                "aws",
                "s3api",
                "get-object",
                "--bucket",
                model_store_bucket,
                "--key",
                model_store_key,
                "/tmp/model-denied",
            ),
            (
                "aws",
                "s3api",
                "put-object",
                "--bucket",
                model_store_bucket,
                "--key",
                denied_model_key,
                "--body",
                "/tmp/input",
            ),
            ("aws", "s3api", "delete-bucket", "--bucket", output_bucket),
            (
                "aws",
                "s3api",
                "put-bucket-versioning",
                "--bucket",
                output_bucket,
                "--versioning-configuration",
                "Status=Suspended",
            ),
        )
        for command in denied_commands:
            h.assert_pod_command_denied(namespace, POD_NAME, *command)
    finally:
        h.run_kubectl("delete", "pod", POD_NAME, "-n", namespace, "--ignore-not-found", "--wait=false", check=False)
        h.s3_delete_object(intake_bucket, input_key)
        h.s3_delete_object(intake_bucket, denied_intake_key)
        h.s3_delete_object(output_bucket, output_key)
        h.s3_delete_object(model_store_bucket, model_store_key)
        h.s3_delete_object(model_store_bucket, denied_model_key)
