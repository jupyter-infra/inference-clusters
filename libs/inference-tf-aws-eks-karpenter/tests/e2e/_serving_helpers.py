"""Shared helpers for the live serving E2E tests.

Used by test_vllm_serving (basic serving), test_kro_graph_serving (Path-B graph
onboarding + KRO CR lifecycle, no Helm), and test_keda_scale_from_zero (KEDA
scale-from-zero). Keeps the onboard/invoke plumbing in one place so the tests differ
only in what they assert. onboard_chart is Path A (Helm chart -> overrides.yaml);
onboard_graph is Path B (KRO graph -> graph-air-gapped.yaml).
"""

import functools
import json
import os
import shutil
import string
import subprocess
import tempfile
import time
from pathlib import Path

import boto3
from mypy_boto3_fsx.client import FSxClient
from mypy_boto3_s3.client import S3Client
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.kubernetes import nodes
from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl

CHARTS_DIR = Path(__file__).resolve().parent / "charts"  # Path-A Helm chart fixtures
GRAPHS_DIR = Path(__file__).resolve().parent / "graphs"  # Path-B KRO graph fixtures (no Chart.yaml)
SOURCES_DIR = Path(__file__).resolve().parent / "sources"  # image-build source-dir fixtures (Dockerfile + context)
# Static YAML manifests the tests kubectl-apply (never inline heredocs in a test body).
RESOURCES_DIR = Path(__file__).resolve().parent / "resources"
NAMESPACE = "default"
# The vLLM image the vllm-qwen chart declares, as the onboarder names it UNDER the
# cluster-scoped workload prefix (<cluster>/workload/...). Used as a substring assertion on
# the emitted overrides (the full ref is <ecr>/<cluster>/workload/vllm/vllm-openai@<digest>).
# For the full repo name (e.g. to delete it), prefix with the workload_repo_prefix output —
# see workload_image_repo().
WORKLOAD_IMAGE_SUFFIX = "workload/vllm/vllm-openai"
# Fixtures reference the JumpStart weight-source bucket by this literal placeholder rather
# than a hardcoded name — the bucket embeds the region (jumpstart-cache-prod-<region>), so
# it can't be derived in-code. Resolved from the env var (set in .env / env.example) and
# substituted into a fixture copy just before packaging (see _stage_fixture).
JUMPSTART_BUCKET_PLACEHOLDER = "${JUMPSTART_PUBLIC_BUCKET_NAME}"


def jumpstart_bucket() -> str:
    """The JumpStart public model-cache bucket for this region, from JUMPSTART_PUBLIC_BUCKET_NAME.

    The name embeds the region (jumpstart-cache-prod-<region>), so it is configured via the
    env var (.env / env.example), never derived — a wrong region silently reads another
    bucket. Required for the weight-import / serving tests."""
    bucket = os.environ.get("JUMPSTART_PUBLIC_BUCKET_NAME")
    if not bucket:
        raise RuntimeError("JUMPSTART_PUBLIC_BUCKET_NAME is not set (see .env / env.example)")
    return bucket


def _stage_fixture(src_dir: Path) -> Path:
    """Copy a chart/graph fixture to a temp dir with the JumpStart bucket placeholder resolved.

    Substitutes the literal ${JUMPSTART_PUBLIC_BUCKET_NAME} token (a plain str.replace, so KRO
    ${schema.*} expressions in a graph.yaml are left untouched) so the packaged artifact points
    at the region's bucket. Returns the staged copy's path (same basename as the source)."""
    tmp = Path(tempfile.mkdtemp(prefix="e2e-fixture-")) / src_dir.name
    shutil.copytree(src_dir, tmp)
    bucket = jumpstart_bucket()
    for path in tmp.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue  # binary fixture file (e.g. a .whl) — no text placeholder to substitute
        if JUMPSTART_BUCKET_PLACEHOLDER in text:
            path.write_text(text.replace(JUMPSTART_BUCKET_PLACEHOLDER, bucket))
    return tmp


def jd_output(e2e: EndToEndDeployment, name: str) -> str:
    """Read a single terraform output through the jd CLI."""
    return e2e.cli.run_command(["jupyter-deploy", "show", "--output", name, "--text"]).stdout.strip()


def workload_image_repo(e2e: EndToEndDeployment, image_suffix: str = WORKLOAD_IMAGE_SUFFIX) -> str:
    """Full cluster-scoped ECR repo name for a vendored workload image.

    The onboarder vendors under the cluster-scoped workload_repo_prefix (<cluster>/workload),
    so a repo name is `<prefix>/<image>` minus the redundant leading 'workload/' of the
    suffix — e.g. prefix 'inference-abc/workload' + suffix 'workload/vllm/vllm-openai' ->
    'inference-abc/workload/vllm/vllm-openai'. Used for teardown (delete the repo this
    deployment created, not a shared account-global one)."""
    prefix = jd_output(e2e, "workload_repo_prefix")  # e.g. inference-<id>/workload
    image = image_suffix.removeprefix("workload/")  # e.g. vllm/vllm-openai
    return f"{prefix}/{image}"


@functools.cache
def _s3_client() -> S3Client:
    """boto3 S3 client for host-side test setup and cleanup, built once per test session."""
    return boto3.client("s3")


@functools.cache
def fsx_client(region: str) -> FSxClient:
    """boto3 FSx client for host-side test polls (region-pinned), one per session per region."""
    return boto3.client("fsx", region_name=region)


def s3_put_object(bucket: str, key: str, body: bytes) -> None:
    """Upload a small test object from the test host."""
    _s3_client().put_object(Bucket=bucket, Key=key, Body=body)


def s3_delete_object(bucket: str, key: str) -> None:
    """Delete a test object from the test host. S3 reports success for an absent key,
    so cleanup of an object that a denied write never created is a no-op."""
    _s3_client().delete_object(Bucket=bucket, Key=key)


def exec_in_pod(namespace: str, pod: str, *command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command in a pod."""
    return run_kubectl("exec", pod, "-n", namespace, "--", *command, check=check)


def assert_pod_command_denied(namespace: str, pod: str, *command: str) -> None:
    """Check that AWS denies a command from a pod."""
    result = exec_in_pod(namespace, pod, *command, check=False)
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, f"The command succeeded but access must be denied: {' '.join(command)}"
    assert "AccessDenied" in output or "not authorized" in output, output


def apply_resource(name: str, **subs: str) -> str:
    """kubectl-apply a manifest from tests/e2e/resources/, substituting any ${...} vars.

    Keeps test YAML out of the test bodies (mirrors the eks-oidc workspaces/ pattern).
    Returns the rendered manifest so the caller can assert on it if needed.
    """
    text = (RESOURCES_DIR / name).read_text()
    if subs:
        text = string.Template(text).substitute(**subs)
    subprocess.run(["kubectl", "apply", "-f", "-"], input=text, text=True, check=True, capture_output=True)
    return text


def _run_onboard_build(
    e2e: EndToEndDeployment, region: str, artifact_key: str, out_name: str, out_basename: str, max_polls: int = 60
) -> Path:
    """Start the onboarder CodeBuild against an already-uploaded artifact tarball,
    poll to completion, and download the emitted artifact (overrides.yaml or
    graph-air-gapped.yaml). Default ceiling ~20 min (60 x 20s) fits image vendor +
    ~15GB weight copy; raise max_polls for a large-weights import.
    """
    project = jd_output(e2e, "onboarder_codebuild_project")
    in_uri = jd_output(e2e, "onboarder_input_s3_uri")
    out_uri = jd_output(e2e, "onboarder_output_s3_uri")

    build_id = subprocess.run(
        [
            "aws",
            "codebuild",
            "start-build",
            "--project-name",
            project,
            "--region",
            region,
            "--environment-variables-override",
            f"name=CHART_REF,value={in_uri}/{artifact_key},type=PLAINTEXT",
            "--query",
            "build.id",
            "--output",
            "text",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    status = "IN_PROGRESS"
    for _ in range(max_polls):
        status = subprocess.run(
            [
                "aws",
                "codebuild",
                "batch-get-builds",
                "--ids",
                build_id,
                "--region",
                region,
                "--query",
                "builds[0].buildStatus",
                "--output",
                "text",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if status != "IN_PROGRESS":
            break
        time.sleep(20)
    assert status == "SUCCEEDED", f"onboarder build {build_id} ended {status}"

    local = Path(f"/tmp/{out_name}-{out_basename}")
    subprocess.run(
        ["aws", "s3", "cp", f"{out_uri}/{out_name}/{out_basename}", str(local)], check=True, capture_output=True
    )
    return local


def onboard_chart(e2e: EndToEndDeployment, region: str, chart_name: str, max_polls: int = 60) -> Path:
    """Path A: package tests/e2e/charts/<chart_name> as a Helm chart, onboard it, return
    the emitted overrides.yaml (output name == the chart's Chart.yaml name == chart_name).
    max_polls raises the build-wait ceiling for a large-weights import."""
    chart_dir = _stage_fixture(CHARTS_DIR / chart_name)
    in_uri = jd_output(e2e, "onboarder_input_s3_uri")
    subprocess.run(["helm", "package", str(chart_dir), "-d", "/tmp"], check=True, capture_output=True)
    tgz = next(Path("/tmp").glob(f"{chart_name}-*.tgz"))
    subprocess.run(["aws", "s3", "cp", str(tgz), f"{in_uri}/{chart_name}.tgz"], check=True, capture_output=True)
    return _run_onboard_build(e2e, region, f"{chart_name}.tgz", chart_name, "overrides.yaml", max_polls=max_polls)


def build_image(
    e2e: EndToEndDeployment, region: str, source_name: str, image_name: str, image_tag: str, max_polls: int = 60
) -> str:
    """Build a source-dir fixture (sources/<source_name>) into <ecr>/workload/<image_name>:<tag>
    via the image-build CodeBuild job, and return the pushed image ref.

    This is the worked example of the image-build contract a consumer follows:
      1. tar the source dir (Dockerfile + any build context / wheels)
      2. upload it to the image-build input prefix (image_build_input_s3_uri)
      3. `aws codebuild start-build` with SOURCE_REF + IMAGE_NAME + IMAGE_TAG overrides
      4. poll to completion
    The source is a RUNTIME S3 upload (never terraform state), so it works across repos.
    """
    project = jd_output(e2e, "image_build_codebuild_project")
    in_uri = jd_output(e2e, "image_build_input_s3_uri")
    registry = jd_output(e2e, "ecr_registry")
    workload_prefix = jd_output(e2e, "workload_repo_prefix")

    # Tar the fixture dir directly — NOT via _stage_fixture: that helper read_text()s
    # every file for placeholder substitution and would choke on binary build context
    # (e.g. a .whl), and an image-build source has no ${JUMPSTART_*} placeholder anyway.
    src_dir = SOURCES_DIR / source_name
    tgz = Path(tempfile.mkdtemp(prefix="e2e-imgbuild-")) / "source.tgz"
    subprocess.run(["tar", "-czf", str(tgz), "-C", str(src_dir), "."], check=True, capture_output=True)
    source_ref = f"{in_uri}/{image_name}/source.tgz"
    subprocess.run(["aws", "s3", "cp", str(tgz), source_ref], check=True, capture_output=True)

    build_id = subprocess.run(
        [
            "aws",
            "codebuild",
            "start-build",
            "--project-name",
            project,
            "--region",
            region,
            "--environment-variables-override",
            f"name=SOURCE_REF,value={source_ref},type=PLAINTEXT",
            f"name=IMAGE_NAME,value={image_name},type=PLAINTEXT",
            f"name=IMAGE_TAG,value={image_tag},type=PLAINTEXT",
            "--query",
            "build.id",
            "--output",
            "text",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    status = "IN_PROGRESS"
    for _ in range(max_polls):
        status = subprocess.run(
            [
                "aws",
                "codebuild",
                "batch-get-builds",
                "--ids",
                build_id,
                "--region",
                region,
                "--query",
                "builds[0].buildStatus",
                "--output",
                "text",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if status != "IN_PROGRESS":
            break
        time.sleep(20)
    assert status == "SUCCEEDED", f"image-build {build_id} ended {status}"
    return f"{registry}/{workload_prefix}/{image_name}:{image_tag}"


def ecr_image_exists(region: str, repository: str, tag: str) -> bool:
    """Whether a specific tag exists in an ECR repo (asserts an image-build published)."""
    r = subprocess.run(
        [
            "aws",
            "ecr",
            "describe-images",
            "--repository-name",
            repository,
            "--region",
            region,
            "--image-ids",
            f"imageTag={tag}",
            "--query",
            "imageDetails[0].imageDigest",
            "--output",
            "text",
        ],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0 and r.stdout.strip() not in ("", "None")


def s3_prefix_stats(uri: str) -> tuple[int, int]:
    """Return (object_count, total_bytes) under an s3:// prefix (recursive), for asserting
    a weights import landed. Uses list-objects-v2 paging via the CLI."""
    bucket, _, prefix = uri[len("s3://") :].partition("/")
    out = subprocess.run(
        [
            "aws",
            "s3api",
            "list-objects-v2",
            "--bucket",
            bucket,
            "--prefix",
            prefix,
            "--query",
            "[sum(Contents[].Size), length(Contents[])]",
            "--output",
            "text",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    total = 0 if out[0] in ("None", "") else int(float(out[0]))
    count = 0 if len(out) < 2 or out[1] in ("None", "") else int(out[1])
    return count, total


def delete_s3_prefix(uri: str) -> None:
    """Recursively delete every object under an s3:// prefix (test cleanup for a large
    weights import, so a 100s-of-GB copy never lingers and accrues cost). check=True so a
    failed purge surfaces loudly rather than silently leaving the objects behind."""
    subprocess.run(["aws", "s3", "rm", "--recursive", uri.rstrip("/") + "/"], check=True, capture_output=True)


def delete_ecr_repo(region: str, repository: str) -> None:
    """Force-delete an ECR repository (and its images) if present; no-op if already gone.

    The onboarder creates workload/* repos imperatively (they are NOT in terraform state,
    so `jd down` does not reap them) — the serving tests use this to clean up what they
    onboarded. --force removes the repo even with images in it. Tolerates
    RepositoryNotFoundException so a re-run or a never-created repo is a no-op."""
    result = subprocess.run(
        ["aws", "ecr", "delete-repository", "--repository-name", repository, "--force", "--region", region],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and "RepositoryNotFoundException" not in result.stderr:
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)


def onboard_graph(e2e: EndToEndDeployment, region: str, graph_name: str, out_name: str) -> Path:
    """Path B: tar tests/e2e/graphs/<graph_name> (graph.yaml + values.yaml), onboard it,
    return the emitted graph-air-gapped.yaml. `out_name` is the graph's metadata.name
    (the rehost/out/<name>/ subdir onboarder.py derives)."""
    graph_dir = _stage_fixture(GRAPHS_DIR / graph_name)
    in_uri = jd_output(e2e, "onboarder_input_s3_uri")
    tgz = Path(f"/tmp/{graph_name}.tgz")
    # --strip-components=1 on unpack expects a single top-level dir; tar with that layout.
    subprocess.run(
        ["tar", "-czf", str(tgz), "-C", str(graph_dir.parent), graph_dir.name], check=True, capture_output=True
    )
    subprocess.run(["aws", "s3", "cp", str(tgz), f"{in_uri}/{graph_name}.tgz"], check=True, capture_output=True)
    return _run_onboard_build(e2e, region, f"{graph_name}.tgz", out_name, "graph-air-gapped.yaml")


def client_image(e2e: EndToEndDeployment) -> str:
    """busybox via ECR pull-through — nodes are air-gapped, a public.ecr.aws ref can't be pulled."""
    registry = jd_output(e2e, "ecr_registry")
    return f"{registry}/ecr-public/docker/library/busybox:1.36"


def python_image(e2e: EndToEndDeployment) -> str:
    """python:3.12-slim via ECR pull-through (ecr-public) — Docker Hub is NOT a pull-through
    upstream, but public.ecr.aws mirrors the official python image. Used for the KEDA
    router (stdlib-only script, no pip)."""
    registry = jd_output(e2e, "ecr_registry")
    return f"{registry}/ecr-public/docker/library/python:3.12-slim"


def aws_cli_image(e2e: EndToEndDeployment) -> str:
    """AWS CLI via ECR pull-through (ecr-public). Test-only image: the batch E2E pod
    uses it to exercise Pod Identity credentials."""
    registry = jd_output(e2e, "ecr_registry")
    return f"{registry}/ecr-public/aws-cli/aws-cli:latest"


def _chat_prompt(model: str) -> str:
    """The tiny deterministic OpenAI chat request both invoke helpers POST (max_tokens=16)."""
    return json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with exactly the word: pong"}],
            "max_tokens": 16,
            "temperature": 0,
        }
    )


def _retry_invoke_script(url: str, prompt: str, attempts: int, read_timeout_s: int, backoff_s: int) -> str:
    """A busybox-sh client script that POSTs `prompt` to `url`, RETRYING until it sees a
    completion (a body containing "choices") or `attempts` runs out.

    The retry loop is the anti-flake: vLLM can reject/reset/hang the first request(s) during
    cold start even after its rollout reports ready (first-token KV-cache init / CUDA graph
    capture). Each attempt gets a `-T read_timeout_s` so one held request can ride out a slow
    first token; on any non-completion (connection refused/reset, empty body, error status)
    it backs off `backoff_s` and tries again. Exits 0 on the first completion, 1 if the whole
    budget (~attempts * (read_timeout tail + backoff)) elapses without one."""
    return (
        "i=0; "
        f"while [ $i -lt {attempts} ]; do "
        f"  r=$(wget -q -O- -T {read_timeout_s} --post-data='{prompt}' "
        f'    --header="Content-Type: application/json" {url}); '
        '  case "$r" in *choices*) echo "$r"; exit 0 ;; esac; '
        '  echo "[client] attempt $i: no completion yet, retrying" 1>&2; '
        f"  i=$((i+1)); sleep {backoff_s}; "
        "done; "
        'echo "[client] gave up after retries" 1>&2; exit 1'
    )


def launch_blocking_invoke(e2e: EndToEndDeployment, service: str, port: int, model: str = "qwen2.5-7b") -> None:
    """Start a busybox client that POSTs /v1/chat/completions and BLOCKS on the response.

    Unlike invoke_chat (run + wait in one call), this returns immediately with the client
    pod still running — so the caller can observe scaling WHILE the request is in flight
    (the router holds the connection through the vLLM cold start). Collect it later with
    collect_blocking_invoke.

    The client SOFT-FAILS and RETRIES (see _retry_invoke_script): the router pod may not be
    ready when busybox starts, and a long-held connection can be reset mid-cold-start. The
    retries are also what drive the router's in-flight gauge repeatedly, which is exactly
    what KEDA needs to see. Budget ~28 min (85 x 20s attempts), -T 1200 per held attempt.
    """
    prompt = _chat_prompt(model)
    url = f"http://{service}.{NAMESPACE}.svc:{port}/v1/chat/completions"
    script = _retry_invoke_script(url, prompt, attempts=85, read_timeout_s=1200, backoff_s=20)
    run_kubectl("delete", "pod", "keda-client", "-n", NAMESPACE, "--ignore-not-found", check=False)
    run_kubectl(
        "run",
        "keda-client",
        "-n",
        NAMESPACE,
        "--restart=Never",
        f"--image={client_image(e2e)}",
        "--",
        "sh",
        "-c",
        script,
        check=True,
    )


def collect_blocking_invoke(timeout_s: int = 1200) -> dict:
    """Wait for the launch_blocking_invoke client to Succeed, assert a completion, clean up."""
    try:
        run_kubectl(
            "wait",
            "--for=jsonpath={.status.phase}=Succeeded",
            "pod/keda-client",
            "-n",
            NAMESPACE,
            f"--timeout={timeout_s}s",
            check=True,
        )
        out = run_kubectl("logs", "keda-client", "-n", NAMESPACE, check=True).stdout
        assert '"choices"' in out, f"expected an OpenAI completion through the router, got:\n{out}"
        body = json.loads(out[out.index("{") :])
        assert body["choices"][0]["message"]["content"].strip(), f"completion must be non-empty, got {body!r}"
        return body
    finally:
        run_kubectl("delete", "pod", "keda-client", "-n", NAMESPACE, "--ignore-not-found", check=False)


def invoke_chat(e2e: EndToEndDeployment, service: str, model: str = "qwen2.5-7b") -> dict:
    """POST /v1/chat/completions from a throwaway client pod; return the parsed OpenAI response.

    Uses busybox+wget over the ClusterIP Service (in-cluster), the only reachable path on
    the endpoints-only VPC. Asserts a non-empty completion.

    The client RETRIES (shared _retry_invoke_script) rather than firing one shot: even after
    `helm rollout status` reports the Deployment ready, vLLM's FIRST request can reset/hang
    during cold start (first-token KV-cache init / CUDA graph capture) — a one-shot wget
    here was the flake that failed this test intermittently. Budget ~10 min (30 x 8s waits +
    a 300s per-attempt read tail); the pod-wait timeout tracks that budget with headroom.
    """
    prompt = _chat_prompt(model)
    url = f"http://{service}.{NAMESPACE}.svc:8000/v1/chat/completions"
    script = _retry_invoke_script(url, prompt, attempts=30, read_timeout_s=300, backoff_s=8)
    run_kubectl("delete", "pod", "vllm-client", "-n", NAMESPACE, "--ignore-not-found", check=False)
    run_kubectl(
        "run",
        "vllm-client",
        "-n",
        NAMESPACE,
        "--restart=Never",
        f"--image={client_image(e2e)}",
        "--",
        "sh",
        "-c",
        script,
        check=True,
    )
    try:
        run_kubectl(
            "wait",
            "--for=jsonpath={.status.phase}=Succeeded",
            "pod/vllm-client",
            "-n",
            NAMESPACE,
            "--timeout=720s",
            check=True,
        )
        out = run_kubectl("logs", "vllm-client", "-n", NAMESPACE, check=True).stdout
        assert '"choices"' in out, f"expected an OpenAI completion, got:\n{out}"
        body = json.loads(out[out.index("{") :])
        content = body["choices"][0]["message"]["content"]
        assert content.strip(), f"completion content must be non-empty, got {body!r}"
        return body
    finally:
        run_kubectl("delete", "pod", "vllm-client", "-n", NAMESPACE, "--ignore-not-found", check=False)


def assert_on_karpenter_gpu(release: str, accelerator: str = "nvidia-g") -> str:
    """Assert the release's pod landed on a Karpenter GPU node; return the node name."""
    node = run_kubectl(
        "get", "pods", "-n", NAMESPACE, "-l", f"app={release}", "-o", "jsonpath={.items[0].spec.nodeName}", check=True
    ).stdout.strip()
    labels = run_kubectl("get", "node", node, "-o", "jsonpath={.metadata.labels}", check=True).stdout
    assert accelerator in labels, f"pod must run on a Karpenter {accelerator} node, got {node} labels {labels}"
    return node


def deployment_names_by_instance(namespace: str, helm_release: str) -> list[str]:
    """Deployment names in a namespace that belong to a Helm release.

    Discovered via the standard app.kubernetes.io/instance=<release> label rather than
    hardcoded — chart fullname logic varies (e.g. cluster-autoscaler renders
    'cluster-autoscaler-aws-cluster-autoscaler'), and a wrong literal name is exactly the
    silent-miss class that hides a broken replica/nodeSelector key."""
    out = run_kubectl(
        "get",
        "deployments",
        "-n",
        namespace,
        "-l",
        f"app.kubernetes.io/instance={helm_release}",
        "-o",
        "jsonpath={.items[*].metadata.name}",
        check=False,
    ).stdout.strip()
    return out.split() if out else []


def assert_deployment_replicas_ready(namespace: str, deployment: str, expected: int) -> None:
    """Assert a Deployment declares AND has ready `expected` replicas.

    Reading BOTH .spec.replicas and .status.readyReplicas is the point: .spec proves the
    chart honored our replica key (a phantom key would leave it at the chart default), and
    .status proves the standbys actually scheduled on the system MNG (not stuck Pending)."""
    spec = run_kubectl(
        "get", "deployment", deployment, "-n", namespace, "-o", "jsonpath={.spec.replicas}", check=True
    ).stdout.strip()
    ready = run_kubectl(
        "get", "deployment", deployment, "-n", namespace, "-o", "jsonpath={.status.readyReplicas}", check=True
    ).stdout.strip()
    assert spec == str(expected), f"{namespace}/{deployment} .spec.replicas={spec}, expected {expected}"
    assert ready == str(expected), (
        f"{namespace}/{deployment} .status.readyReplicas={ready}, expected {expected} (standby not scheduled?)"
    )


def system_node_names() -> list[str]:
    """Names of the Ready system-MNG nodes (inference/role=system label)."""
    return nodes.get_node_names("inference/role=system")


def system_node_allocatable_cpu_millicores() -> int:
    """Allocatable CPU (millicores) of the first system node — the per-node sizing unit.

    Ballast CPU requests are derived from this so the scale-up test isn't hardcoded to a
    specific instance type (a fixed request would either never trigger scale-up on a large
    SKU or over-trigger on a small one)."""
    system_nodes = system_node_names()
    assert system_nodes, "no system-MNG nodes found (inference/role=system)"
    return nodes.get_node_allocatable_cpu_millicores(system_nodes[0])


def assert_pods_by_selector_on_system_mng(
    namespace: str, selector: str, description: str, exclude_name_substrings: tuple[str, ...] = ()
) -> None:
    """Assert every pod matching a label selector runs on a tainted system-MNG node.

    System nodes carry inference/role=system; Karpenter inference nodes do not. A control
    -loop / addon-controller pod drifting off the system MNG (missing nodeSelector) is a
    silent placement regression the deployment succeeding would not catch.

    Reads each pod's name + .spec.nodeName as pairs (not just the node list) so a pod stuck
    Pending — nodeName empty — is caught as a failure rather than silently skipped: an
    unschedulable controller is exactly the mis-placement (bad nodeSelector/taint) this
    guards against (issue #14).

    exclude_name_substrings drops pods whose name contains any of the substrings — used for
    releases that legitimately mix system-pinned pods with tolerate-all DaemonSets (e.g.
    kube-prometheus-stack's node-exporter, which MUST run on every node)."""
    # name,nodeName per pod: a Pending pod yields "name," (empty nodeName) — an explicit fail.
    raw = run_kubectl(
        "get",
        "pods",
        "-n",
        namespace,
        "-l",
        selector,
        "-o",
        r"jsonpath={range .items[*]}{.metadata.name},{.spec.nodeName}{'\n'}{end}",
        check=True,
    ).stdout.strip()
    pods = [
        (name, node)
        for line in raw.splitlines()
        if line
        for name, _, node in [line.partition(",")]
        if not any(sub in name for sub in exclude_name_substrings)
    ]
    assert pods, f"no pods found for {description} ({selector}) in {namespace}"

    pending = [name for name, node in pods if not node]
    assert not pending, f"{description} pod(s) {pending} are Pending (unschedulable) — not on the system MNG"

    for _, node in pods:
        labels = run_kubectl("get", "node", node, "-o", "jsonpath={.metadata.labels}", check=True).stdout
        assert '"inference/role":"system"' in labels, (
            f"{description} pod on {node} is NOT on the system MNG (labels: {labels[:200]})"
        )


def assert_pods_on_system_mng(namespace: str, helm_release: str) -> None:
    """Assert every pod of a Helm release runs on a tainted system-MNG node."""
    assert_pods_by_selector_on_system_mng(
        namespace, f"app.kubernetes.io/instance={helm_release}", f"release {helm_release}"
    )
