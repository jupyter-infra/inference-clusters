#!/usr/bin/env python3
"""Take down and delete EKS Karpenter e2e project(s) from the S3 store.

Usage:
    scripts/find_takedown_karpenter.py <project-dir> [--deployment-id <id>]
        [--verify-teardown | --no-verify-teardown]

With --deployment-id: reap only that one project (the standard e2e flow tears down its
OWN deployment, so parallel runs never touch each other). Teardown verification is
enabled by default in this mode.

With --verify-teardown: seed a non-empty repository under this deployment's workload
prefix before destroy, then assert that the cluster, VPC, CNI ENIs, generated EKS
security group, and every workload repository are gone. This requires --deployment-id.
Use --no-verify-teardown to explicitly disable it for a scoped teardown.

Without --deployment-id: reap ALL `tf-aws-eks-karpenter-*` projects — the nuclear option
for the standalone cleanup workflow, to clear orphans from interrupted runs. NOT used by
the standard e2e flow.

For each target: restore locally, `jd down -y`, delete from the store. Exits 0 if there
is nothing to take down. Uses the local scripts/ci_helpers.py to drive the published `jd`
CLI installed in this workspace.
"""

from __future__ import annotations

import argparse
import base64
import io
import subprocess
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.client import BaseClient
from botocore.exceptions import ClientError
from ci_helpers import is_project_deployed, run_jd
from ci_restore_karpenter import (
    KARPENTER_PROJECT_PREFIX,
    list_karpenter_projects,
    restore_project,
    restore_secrets,
)

VERIFY_ATTEMPTS = 12
VERIFY_DELAY_SECONDS = 10
CANARY_REPOSITORY_SUFFIX = "teardown-verification"
CANARY_IMAGE_TAG = "issue-36"


@dataclass(frozen=True)
class DeploymentResources:
    region: str
    cluster_name: str
    vpc_id: str
    ecr_registry: str
    workload_repo_prefix: str


@dataclass(frozen=True)
class AwsClients:
    eks: BaseClient
    ec2: BaseClient
    ecr: BaseClient


def create_aws_clients(region: str) -> AwsClients:
    return AwsClients(
        eks=boto3.client("eks", region_name=region),
        ec2=boto3.client("ec2", region_name=region),
        ecr=boto3.client("ecr", region_name=region),
    )


def verification_enabled(deployment_id: str | None, override: bool | None) -> bool:
    """Verify scoped teardown by default, while leaving cleanup-all unverified."""
    return deployment_id is not None if override is None else override


def jd_output(project_dir: Path, name: str) -> str:
    result = run_jd(
        ["show", "--output", name, "--text"],
        cwd=str(project_dir),
        capture=True,
    )
    value = result.stdout.strip()
    if not value:
        raise RuntimeError(f"Terraform output {name!r} is empty")
    return value


def capture_deployment_resources(project_dir: Path) -> DeploymentResources:
    """Capture identifiers that disappear from Terraform state during destroy."""
    return DeploymentResources(
        region=jd_output(project_dir, "region"),
        cluster_name=jd_output(project_dir, "cluster_name"),
        vpc_id=jd_output(project_dir, "vpc_id"),
        ecr_registry=jd_output(project_dir, "ecr_registry").removeprefix("https://"),
        workload_repo_prefix=jd_output(project_dir, "workload_repo_prefix"),
    )


def seed_nonempty_workload_repository(resources: DeploymentResources, ecr: BaseClient) -> str:
    """Push a tiny local image so teardown proves force deletion of a non-empty repo."""
    repository = f"{resources.workload_repo_prefix}/{CANARY_REPOSITORY_SUFFIX}"
    image_ref = f"{resources.ecr_registry}/{repository}:{CANARY_IMAGE_TAG}"

    try:
        ecr.create_repository(repositoryName=repository)
    except ClientError as error:
        if error.response["Error"]["Code"] != "RepositoryAlreadyExistsException":
            raise

    authorization = ecr.get_authorization_token()["authorizationData"][0]
    username, password = base64.b64decode(authorization["authorizationToken"]).decode().split(":", 1)
    subprocess.run(
        ["docker", "login", "--username", username, "--password-stdin", authorization["proxyEndpoint"]],
        input=password,
        text=True,
        check=True,
    )

    payload = b"issue 36 teardown verification\n"
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as image_root:
        info = tarfile.TarInfo("issue-36.txt")
        info.size = len(payload)
        image_root.addfile(info, io.BytesIO(payload))
    subprocess.run(["docker", "import", "-", image_ref], input=archive.getvalue(), check=True)
    subprocess.run(["docker", "push", image_ref], check=True)

    images = ecr.describe_images(repositoryName=repository, imageIds=[{"imageTag": CANARY_IMAGE_TAG}])["imageDetails"]
    if not images:
        raise RuntimeError(f"Canary image was not published to {image_ref}")
    print(f"Seeded non-empty teardown canary: {image_ref}")
    return repository


def remaining_deployment_resources(resources: DeploymentResources, clients: AwsClients) -> list[str]:
    """Return deployment-scoped AWS resources that still exist."""
    remaining: list[str] = []

    try:
        clients.eks.describe_cluster(name=resources.cluster_name)
        remaining.append(f"EKS cluster {resources.cluster_name}")
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    try:
        vpcs = clients.ec2.describe_vpcs(VpcIds=[resources.vpc_id])["Vpcs"]
        remaining.extend(f"VPC {vpc['VpcId']}" for vpc in vpcs)
    except ClientError as error:
        if error.response["Error"]["Code"] != "InvalidVpcID.NotFound":
            raise

    enis = clients.ec2.describe_network_interfaces(
        Filters=[
            {"Name": "vpc-id", "Values": [resources.vpc_id]},
            {"Name": "description", "Values": ["aws-K8S-*"]},
        ]
    )["NetworkInterfaces"]
    remaining.extend(f"CNI ENI {eni['NetworkInterfaceId']}" for eni in enis)

    security_groups = clients.ec2.describe_security_groups(
        Filters=[
            {"Name": "vpc-id", "Values": [resources.vpc_id]},
            {"Name": "group-name", "Values": [f"eks-cluster-sg-{resources.cluster_name}-*"]},
        ]
    )["SecurityGroups"]
    remaining.extend(f"EKS security group {group['GroupId']}" for group in security_groups)

    repository_prefix = f"{resources.workload_repo_prefix}/"
    paginator = clients.ecr.get_paginator("describe_repositories")
    for page in paginator.paginate():
        remaining.extend(
            f"ECR repository {repository['repositoryName']}"
            for repository in page["repositories"]
            if repository["repositoryName"].startswith(repository_prefix)
        )

    return remaining


def verify_teardown(resources: DeploymentResources, clients: AwsClients) -> None:
    """Wait for AWS read-after-delete consistency, then fail on any scoped residue."""
    for attempt in range(1, VERIFY_ATTEMPTS + 1):
        remaining = remaining_deployment_resources(resources, clients)
        if not remaining:
            print("Verified teardown: cluster, VPC, CNI ENIs, EKS security group, and workload repos are gone.")
            return
        if attempt < VERIFY_ATTEMPTS:
            print(f"Teardown verification attempt {attempt}/{VERIFY_ATTEMPTS}: {', '.join(remaining)}")
            time.sleep(VERIFY_DELAY_SECONDS)

    details = "\n  ".join(remaining)
    raise RuntimeError(f"Deployment-scoped resources remain after teardown:\n  {details}")


def takedown_project(project_dir: Path) -> None:
    print(f"Taking down deployment in {project_dir}...")
    result = subprocess.run(["uv", "run", "jd", "down", "-y", "-v"], cwd=project_dir)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def delete_project_from_store(project_id: str) -> None:
    print(f"Deleting project {project_id} from the S3 store...")
    subprocess.run(
        ["uv", "run", "jd", "projects", "delete", project_id, "--store-type", "s3-only", "-y"],
        check=True,
    )


def reap(project_id: str, project_dir: Path, *, verify: bool = False) -> None:
    print(f"\n=== {project_id} ===")
    restore_project(project_id, project_dir)

    if not is_project_deployed(str(project_dir)):
        print(f"  {project_id} has no live infrastructure (empty state) — skipping jd down.")
        delete_project_from_store(project_id)
        print(f"  Stale project {project_id} deleted from store.")
        return

    # Takedown uses destroy.tfvars and never reads a restored secret value, so a
    # missing secret must not block teardown.
    print("  Restoring secrets from the cloud provider...")
    restore_secrets(project_dir, required=False)

    resources = capture_deployment_resources(project_dir) if verify else None
    clients = create_aws_clients(resources.region) if resources is not None else None
    if resources is not None:
        assert clients is not None
        seed_nonempty_workload_repository(resources, clients.ecr)

    takedown_project(project_dir)
    if resources is not None:
        assert clients is not None
        verify_teardown(resources, clients)
    delete_project_from_store(project_id)
    print(f"  Project {project_id} taken down and deleted.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Take down + delete karpenter e2e project(s).")
    parser.add_argument("project_dir", nargs="?", default="sandbox-e2e", help="Directory to restore into")
    parser.add_argument(
        "--deployment-id",
        default=None,
        help="Reap only this deployment's project (default: reap ALL — nuclear cleanup)",
    )
    parser.add_argument(
        "--verify-teardown",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="verify scoped teardown (default: enabled with --deployment-id, disabled for cleanup-all)",
    )
    args = parser.parse_args()

    verify_teardown_enabled = verification_enabled(args.deployment_id, args.verify_teardown)

    if verify_teardown_enabled and not args.deployment_id:
        parser.error("--verify-teardown requires --deployment-id")

    project_dir = Path(args.project_dir)

    targets = [f"{KARPENTER_PROJECT_PREFIX}{args.deployment_id}"] if args.deployment_id else list_karpenter_projects()

    if not targets:
        print(f"No {KARPENTER_PROJECT_PREFIX}* project found — nothing to take down.")
        return

    scope = "own deployment" if args.deployment_id else f"ALL ({len(targets)} project(s))"
    print(f"Reaping {scope}: {targets}")

    for project_id in targets:
        reap(project_id, project_dir, verify=verify_teardown_enabled)

    print("\nDone.")


if __name__ == "__main__":
    main()
