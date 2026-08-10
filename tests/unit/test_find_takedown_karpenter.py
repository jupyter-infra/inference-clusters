"""Tests for deployment-scoped Karpenter teardown verification."""

import base64
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import find_takedown_karpenter as ftk


def _resources() -> ftk.DeploymentResources:
    return ftk.DeploymentResources(
        region="us-west-2",
        cluster_name="inference-abc123",
        vpc_id="vpc-123",
        ecr_registry="123456789012.dkr.ecr.us-west-2.amazonaws.com",
        workload_repo_prefix="inference-abc123/workload",
    )


class TestCaptureDeploymentResources(unittest.TestCase):
    @patch("find_takedown_karpenter.jd_output")
    def test_captures_destroyed_outputs_and_normalizes_registry(self, mock_output: Mock) -> None:
        values = {
            "region": "us-west-2",
            "cluster_name": "inference-abc123",
            "vpc_id": "vpc-123",
            "ecr_registry": "https://123456789012.dkr.ecr.us-west-2.amazonaws.com",
            "workload_repo_prefix": "inference-abc123/workload",
        }
        mock_output.side_effect = lambda _project_dir, name: values[name]

        resources = ftk.capture_deployment_resources(Path("sandbox-e2e"))

        self.assertEqual(resources, _resources())


class TestSeedNonemptyWorkloadRepository(unittest.TestCase):
    @patch("find_takedown_karpenter.subprocess.run")
    @patch("find_takedown_karpenter.boto3.client")
    def test_pushes_tiny_image_under_deployment_prefix(self, mock_client: Mock, mock_run: Mock) -> None:
        ecr = mock_client.return_value
        token = base64.b64encode(b"AWS:password").decode()
        ecr.get_authorization_token.return_value = {
            "authorizationData": [
                {
                    "authorizationToken": token,
                    "proxyEndpoint": "https://123456789012.dkr.ecr.us-west-2.amazonaws.com",
                }
            ]
        }
        ecr.describe_images.return_value = {"imageDetails": [{"imageDigest": "sha256:abc"}]}

        repository = f"{_resources().workload_repo_prefix}/{ftk.CANARY_REPOSITORY_SUFFIX}"
        result = ftk.seed_nonempty_workload_repository(_resources())

        self.assertEqual(result, repository)
        ecr.create_repository.assert_called_once_with(repositoryName=repository)
        ecr.describe_images.assert_called_once_with(
            repositoryName=repository,
            imageIds=[{"imageTag": ftk.CANARY_IMAGE_TAG}],
        )
        commands = [entry.args[0] for entry in mock_run.call_args_list]
        self.assertEqual(commands[0][0:2], ["docker", "login"])
        self.assertEqual(commands[1][0:3], ["docker", "import", "-"])
        self.assertEqual(commands[2][0:2], ["docker", "push"])
        self.assertGreater(len(mock_run.call_args_list[1].kwargs["input"]), 0)


class TestRemainingDeploymentResources(unittest.TestCase):
    @patch("find_takedown_karpenter.boto3.client")
    def test_reports_only_deployment_scoped_resources(self, mock_client: Mock) -> None:
        eks = Mock()
        ec2 = Mock()
        ecr = Mock()
        mock_client.side_effect = [eks, ec2, ecr]
        ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-123"}]}
        ec2.describe_network_interfaces.return_value = {"NetworkInterfaces": [{"NetworkInterfaceId": "eni-123"}]}
        ec2.describe_security_groups.return_value = {"SecurityGroups": [{"GroupId": "sg-123"}]}
        paginator = ecr.get_paginator.return_value
        paginator.paginate.return_value = [
            {
                "repositories": [
                    {"repositoryName": "inference-abc123/workload/canary"},
                    {"repositoryName": "inference-other/workload/canary"},
                    {"repositoryName": "inference-abc123/platform"},
                ]
            }
        ]

        remaining = ftk.remaining_deployment_resources(_resources())

        self.assertEqual(
            remaining,
            [
                "EKS cluster inference-abc123",
                "VPC vpc-123",
                "CNI ENI eni-123",
                "EKS security group sg-123",
                "ECR repository inference-abc123/workload/canary",
            ],
        )
        ec2.describe_network_interfaces.assert_called_once_with(
            Filters=[
                {"Name": "vpc-id", "Values": ["vpc-123"]},
                {"Name": "description", "Values": ["aws-K8S-*"]},
            ]
        )


class TestVerifyTeardown(unittest.TestCase):
    @patch("find_takedown_karpenter.time.sleep")
    @patch("find_takedown_karpenter.remaining_deployment_resources")
    def test_retries_until_all_resources_are_gone(self, mock_remaining: Mock, mock_sleep: Mock) -> None:
        mock_remaining.side_effect = [["VPC vpc-123"], []]

        ftk.verify_teardown(_resources())

        self.assertEqual(mock_remaining.call_count, 2)
        mock_sleep.assert_called_once_with(ftk.VERIFY_DELAY_SECONDS)

    @patch("find_takedown_karpenter.time.sleep")
    @patch("find_takedown_karpenter.remaining_deployment_resources")
    def test_fails_with_remaining_resource_details(self, mock_remaining: Mock, mock_sleep: Mock) -> None:
        mock_remaining.return_value = ["ECR repository inference-abc123/workload/canary"]

        with self.assertRaisesRegex(RuntimeError, "workload/canary"):
            ftk.verify_teardown(_resources())

        self.assertEqual(mock_remaining.call_count, ftk.VERIFY_ATTEMPTS)
        self.assertEqual(mock_sleep.call_count, ftk.VERIFY_ATTEMPTS - 1)


class TestReap(unittest.TestCase):
    @patch("find_takedown_karpenter.delete_project_from_store")
    @patch("find_takedown_karpenter.verify_teardown")
    @patch("find_takedown_karpenter.takedown_project")
    @patch("find_takedown_karpenter.seed_nonempty_workload_repository")
    @patch("find_takedown_karpenter.capture_deployment_resources")
    @patch("find_takedown_karpenter.restore_secrets")
    @patch("find_takedown_karpenter.is_project_deployed", return_value=True)
    @patch("find_takedown_karpenter.restore_project")
    def test_verified_reap_seeds_destroys_verifies_then_deletes_store(
        self,
        mock_restore: Mock,
        _mock_deployed: Mock,
        mock_secrets: Mock,
        mock_capture: Mock,
        mock_seed: Mock,
        mock_takedown: Mock,
        mock_verify: Mock,
        mock_delete: Mock,
    ) -> None:
        project_dir = Path("sandbox-e2e")
        resources = _resources()
        mock_capture.return_value = resources
        manager = Mock()
        manager.attach_mock(mock_seed, "seed")
        manager.attach_mock(mock_takedown, "takedown")
        manager.attach_mock(mock_verify, "verify")
        manager.attach_mock(mock_delete, "delete")

        ftk.reap("tf-aws-eks-karpenter-abc123", project_dir, verify=True)

        mock_restore.assert_called_once_with("tf-aws-eks-karpenter-abc123", project_dir)
        mock_secrets.assert_called_once_with(project_dir, required=False)
        self.assertEqual(
            manager.mock_calls,
            [
                call.seed(resources),
                call.takedown(project_dir),
                call.verify(resources),
                call.delete("tf-aws-eks-karpenter-abc123"),
            ],
        )

    @patch("find_takedown_karpenter.delete_project_from_store")
    @patch("find_takedown_karpenter.verify_teardown")
    @patch("find_takedown_karpenter.takedown_project")
    @patch("find_takedown_karpenter.seed_nonempty_workload_repository")
    @patch("find_takedown_karpenter.capture_deployment_resources")
    @patch("find_takedown_karpenter.restore_secrets")
    @patch("find_takedown_karpenter.is_project_deployed", return_value=True)
    @patch("find_takedown_karpenter.restore_project")
    def test_unverified_reap_preserves_reaper_behavior(
        self,
        _mock_restore: Mock,
        _mock_deployed: Mock,
        _mock_secrets: Mock,
        mock_capture: Mock,
        mock_seed: Mock,
        mock_takedown: Mock,
        mock_verify: Mock,
        mock_delete: Mock,
    ) -> None:
        project_dir = Path("sandbox-e2e")

        ftk.reap("tf-aws-eks-karpenter-abc123", project_dir)

        mock_capture.assert_not_called()
        mock_seed.assert_not_called()
        mock_takedown.assert_called_once_with(project_dir)
        mock_verify.assert_not_called()
        mock_delete.assert_called_once_with("tf-aws-eks-karpenter-abc123")


class TestJdOutput(unittest.TestCase):
    @patch("find_takedown_karpenter.run_jd")
    def test_reads_output_from_restored_project(self, mock_run: Mock) -> None:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="us-west-2\n")

        self.assertEqual(ftk.jd_output(Path("sandbox-e2e"), "region"), "us-west-2")

        self.assertEqual(
            mock_run.call_args.args[0],
            ["show", "--output", "region", "--text"],
        )
        self.assertEqual(mock_run.call_args.kwargs["cwd"], "sandbox-e2e")
