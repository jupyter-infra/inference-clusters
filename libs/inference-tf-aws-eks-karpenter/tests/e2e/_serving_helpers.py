"""Shared helpers for the live serving E2E tests.

Used by test_vllm_serving (basic serving), test_kro_graph_serving (Path-B graph
onboarding + KRO CR lifecycle, no Helm), and test_keda_scale_from_zero (KEDA
scale-from-zero). Keeps the onboard/invoke plumbing in one place so the tests differ
only in what they assert. onboard_chart is Path A (Helm chart -> overrides.yaml);
onboard_graph is Path B (KRO graph -> graph-air-gapped.yaml).
"""

import json
import os
import shutil
import string
import subprocess
import tempfile
import time
from pathlib import Path

from pytest_jupyter_deploy.deployment import EndToEndDeployment

CHARTS_DIR = Path(__file__).resolve().parent / "charts"  # Path-A Helm chart fixtures
GRAPHS_DIR = Path(__file__).resolve().parent / "graphs"  # Path-B KRO graph fixtures (no Chart.yaml)
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
        if path.is_file() and JUMPSTART_BUCKET_PLACEHOLDER in (text := path.read_text()):
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


def kubectl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["kubectl", *args], check=check, capture_output=True, text=True)


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


def launch_blocking_invoke(e2e: EndToEndDeployment, service: str, port: int, model: str = "qwen2.5-7b") -> None:
    """Start a busybox client that POSTs /v1/chat/completions and BLOCKS on the response.

    Unlike invoke_chat (run + wait in one call), this returns immediately with the client
    pod still running — so the caller can observe scaling WHILE the request is in flight
    (the router holds the connection through the vLLM cold start). Collect it later with
    collect_blocking_invoke.

    The client SOFT-FAILS and RETRIES: the router pod may not be ready when busybox
    starts, and a long-held connection can be reset mid-cold-start. So we loop — each
    attempt gives the router a long read timeout; on any non-completion (connection
    refused/reset, empty body, error status) we sleep and retry until a response with
    "choices" arrives or the overall budget elapses. The retries are what drive the
    router's in-flight gauge repeatedly, which is exactly what KEDA needs to see.
    """
    prompt = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with exactly the word: pong"}],
            "max_tokens": 16,
            "temperature": 0,
        }
    )
    url = f"http://{service}.{NAMESPACE}.svc:{port}/v1/chat/completions"
    # Retry loop in the client: keep hitting the router until we get a real completion or
    # the budget (~28 min of 20s attempts) runs out. -T 1200 lets a single held attempt
    # ride out the vLLM cold start; on failure we back off 20s and try again.
    script = (
        "i=0; "
        "while [ $i -lt 85 ]; do "
        f"  r=$(wget -q -O- -T 1200 --post-data='{prompt}' "
        f'    --header="Content-Type: application/json" {url}); '
        '  case "$r" in *choices*) echo "$r"; exit 0 ;; esac; '
        '  echo "[client] attempt $i: no completion yet, retrying" 1>&2; '
        "  i=$((i+1)); sleep 20; "
        "done; "
        'echo "[client] gave up after retries" 1>&2; exit 1'
    )
    kubectl("delete", "pod", "keda-client", "-n", NAMESPACE, "--ignore-not-found", check=False)
    kubectl(
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
    )


def collect_blocking_invoke(timeout_s: int = 1200) -> dict:
    """Wait for the launch_blocking_invoke client to Succeed, assert a completion, clean up."""
    try:
        kubectl(
            "wait",
            "--for=jsonpath={.status.phase}=Succeeded",
            "pod/keda-client",
            "-n",
            NAMESPACE,
            f"--timeout={timeout_s}s",
        )
        out = kubectl("logs", "keda-client", "-n", NAMESPACE).stdout
        assert '"choices"' in out, f"expected an OpenAI completion through the router, got:\n{out}"
        body = json.loads(out[out.index("{") :])
        assert body["choices"][0]["message"]["content"].strip(), f"completion must be non-empty, got {body!r}"
        return body
    finally:
        kubectl("delete", "pod", "keda-client", "-n", NAMESPACE, "--ignore-not-found", check=False)


def invoke_chat(e2e: EndToEndDeployment, service: str, model: str = "qwen2.5-7b") -> dict:
    """POST /v1/chat/completions from a throwaway client pod; return the parsed OpenAI response.

    Uses busybox+wget over the ClusterIP Service (in-cluster), the only reachable path on
    the endpoints-only VPC. Asserts a non-empty completion.
    """
    prompt = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with exactly the word: pong"}],
            "max_tokens": 16,
            "temperature": 0,
        }
    )
    url = f"http://{service}.{NAMESPACE}.svc:8000/v1/chat/completions"
    kubectl("delete", "pod", "vllm-client", "-n", NAMESPACE, "--ignore-not-found", check=False)
    kubectl(
        "run",
        "vllm-client",
        "-n",
        NAMESPACE,
        "--restart=Never",
        f"--image={client_image(e2e)}",
        "--",
        "sh",
        "-c",
        f"wget -q -O- --post-data='{prompt}' --header=\"Content-Type: application/json\" {url}",
    )
    try:
        kubectl(
            "wait", "--for=jsonpath={.status.phase}=Succeeded", "pod/vllm-client", "-n", NAMESPACE, "--timeout=120s"
        )
        out = kubectl("logs", "vllm-client", "-n", NAMESPACE).stdout
        assert '"choices"' in out, f"expected an OpenAI completion, got:\n{out}"
        body = json.loads(out[out.index("{") :])
        content = body["choices"][0]["message"]["content"]
        assert content.strip(), f"completion content must be non-empty, got {body!r}"
        return body
    finally:
        kubectl("delete", "pod", "vllm-client", "-n", NAMESPACE, "--ignore-not-found", check=False)


def assert_on_karpenter_gpu(release: str, accelerator: str = "nvidia-g") -> str:
    """Assert the release's pod landed on a Karpenter GPU node; return the node name."""
    node = kubectl(
        "get", "pods", "-n", NAMESPACE, "-l", f"app={release}", "-o", "jsonpath={.items[0].spec.nodeName}"
    ).stdout.strip()
    labels = kubectl("get", "node", node, "-o", "jsonpath={.metadata.labels}").stdout
    assert accelerator in labels, f"pod must run on a Karpenter {accelerator} node, got {node} labels {labels}"
    return node


def deployment_names_by_instance(namespace: str, helm_release: str) -> list[str]:
    """Deployment names in a namespace that belong to a Helm release.

    Discovered via the standard app.kubernetes.io/instance=<release> label rather than
    hardcoded — chart fullname logic varies (e.g. cluster-autoscaler renders
    'cluster-autoscaler-aws-cluster-autoscaler'), and a wrong literal name is exactly the
    silent-miss class that hides a broken replica/nodeSelector key."""
    out = kubectl(
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
    spec = kubectl("get", "deployment", deployment, "-n", namespace, "-o", "jsonpath={.spec.replicas}").stdout.strip()
    ready = kubectl(
        "get", "deployment", deployment, "-n", namespace, "-o", "jsonpath={.status.readyReplicas}"
    ).stdout.strip()
    assert spec == str(expected), f"{namespace}/{deployment} .spec.replicas={spec}, expected {expected}"
    assert ready == str(expected), (
        f"{namespace}/{deployment} .status.readyReplicas={ready}, expected {expected} (standby not scheduled?)"
    )


def system_node_names() -> list[str]:
    """Names of the Ready system-MNG nodes (inference/role=system label)."""
    out = kubectl(
        "get", "nodes", "-l", "inference/role=system", "-o", "jsonpath={.items[*].metadata.name}", check=False
    ).stdout.strip()
    return out.split() if out else []


def _parse_cpu_to_millicores(quantity: str) -> int:
    """Parse a k8s CPU quantity ('2', '1930m') to integer millicores."""
    quantity = quantity.strip()
    if quantity.endswith("m"):
        return int(quantity[:-1])
    return int(float(quantity) * 1000)


def system_node_allocatable_cpu_millicores() -> int:
    """Allocatable CPU (millicores) of the first system node — the per-node sizing unit.

    Ballast CPU requests are derived from this so the scale-up test isn't hardcoded to a
    specific instance type (a fixed request would either never trigger scale-up on a large
    SKU or over-trigger on a small one)."""
    nodes = system_node_names()
    assert nodes, "no system-MNG nodes found (inference/role=system)"
    cpu = kubectl("get", "node", nodes[0], "-o", "jsonpath={.status.allocatable.cpu}").stdout.strip()
    return _parse_cpu_to_millicores(cpu)


def assert_pods_by_selector_on_system_mng(namespace: str, selector: str, description: str) -> None:
    """Assert every pod matching a label selector runs on a tainted system-MNG node.

    System nodes carry inference/role=system; Karpenter inference nodes do not. A control
    -loop / addon-controller pod drifting off the system MNG (missing nodeSelector) is a
    silent placement regression the deployment succeeding would not catch."""
    nodes = (
        kubectl("get", "pods", "-n", namespace, "-l", selector, "-o", "jsonpath={.items[*].spec.nodeName}")
        .stdout.strip()
        .split()
    )
    assert nodes, f"no pods found for {description} ({selector}) in {namespace}"
    for node in set(nodes):
        labels = kubectl("get", "node", node, "-o", "jsonpath={.metadata.labels}").stdout
        assert '"inference/role":"system"' in labels, (
            f"{description} pod on {node} is NOT on the system MNG (labels: {labels[:200]})"
        )


def assert_pods_on_system_mng(namespace: str, helm_release: str) -> None:
    """Assert every pod of a Helm release runs on a tainted system-MNG node."""
    assert_pods_by_selector_on_system_mng(
        namespace, f"app.kubernetes.io/instance={helm_release}", f"release {helm_release}"
    )
