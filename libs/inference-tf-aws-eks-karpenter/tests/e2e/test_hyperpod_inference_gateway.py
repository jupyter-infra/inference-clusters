"""Live E2E — the opt-in SageMaker HyperPod Inference Gateway.

Proves the contract `enable_hyperpod_inference_gateway` promises:
  1. the `amazon-sagemaker-hyperpod-inference` add-on is ACTIVE with no health issues, and its
     operator + gateway controller Deployments are available;
  2. the default InferenceGatewayConfig reports Ready (the controller generated the Envoy
     Gateway, BBR and the per-scheduler EPP);
  3. a real model (the vllm-qwen fixture, served under the default scheduler's model name and
     labeled into its InferencePool) answers OpenAI chat completions sent THROUGH the gateway
     (Envoy -> BBR on body `model` -> llm-d EPP -> pod), and
  4. the gateway's own routing metrics, scraped by the in-cluster Prometheus, increase by at
     least the number of requests sent.

Module fixture flips the flag on only when the project does not already have it (and reverts
only what it flipped). Marked `full_deployment` + `mutating`; the serving case is also `gpu`.
"""

import json
import subprocess
import time
import urllib.parse
from collections.abc import Generator

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl

from tests.e2e import _serving_helpers as h

ADDON_NAME = "amazon-sagemaker-hyperpod-inference"
RELEASE = "vllm-gateway-e2e"
CLIENT_POD = "gateway-client"
PROMETHEUS_PROXY = "/api/v1/namespaces/monitoring/services/kube-prometheus-stack-prometheus:9090/proxy"
REQUESTS_SENT = 5
SYSTEM_NG_OVERRIDES = {
    "spec": {
        "nodeSelector": {"inference/role": "system"},
        "tolerations": [{"key": "inference/role", "operator": "Equal", "value": "system", "effect": "NoSchedule"}],
    }
}

# The gateway's REAL metric names, as observed live (add-on v2.1.0-eksbuild.1, llm-d EPP):
# per-model counters on the scheduler's EPP pods, labeled model_name / target_model_name.
# Each MUST grow by at least REQUESTS_SENT once the requests have gone through the gateway.
POOL_REQUEST_METRICS = (
    "inference_objective_request_total",
    "inference_objective_request_duration_seconds_count",
    "inference_objective_input_tokens_count",
    "inference_objective_output_tokens_count",
    "llm_d_epp_request_total",
)
# The shared Body-Based Router counts every request whose body `model` it resolved
# (no model label: it is model-agnostic).
BBR_SUCCESS_METRIC = "bbr_success_total"


@pytest.fixture(scope="module")
def gateway(e2e_deployment: EndToEndDeployment) -> Generator[EndToEndDeployment, None, None]:
    """Ensure the gateway is enabled for the module; revert only if this fixture enabled it."""
    e2e_deployment.ensure_deployed()
    flipped = h.jd_output(e2e_deployment, "hyperpod_inference_gateway_enabled") != "true"
    if flipped:
        e2e_deployment.update_override_value("enable_hyperpod_inference_gateway", True)
        e2e_deployment.ensure_deployed_with([], timeout_seconds=2400)
    e2e_deployment.cli.run_command(["jupyter-deploy", "cluster", "login"])
    try:
        yield e2e_deployment
    finally:
        if flipped:
            e2e_deployment.update_override_value("enable_hyperpod_inference_gateway", False)
            e2e_deployment.ensure_deployed_with([], timeout_seconds=2400)


def _describe_addon(e2e: EndToEndDeployment) -> dict:
    out = subprocess.run(
        [
            "aws",
            "eks",
            "describe-addon",
            "--cluster-name",
            h.jd_output(e2e, "cluster_name"),
            "--addon-name",
            ADDON_NAME,
            "--region",
            h.jd_output(e2e, "region"),
            "--output",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    addon: dict = json.loads(out)["addon"]
    return addon


def _igc(e2e: EndToEndDeployment) -> dict:
    name = h.jd_output(e2e, "hyperpod_inference_gateway_config_name")
    out = run_kubectl("get", "inferencegatewayconfig", name, "-n", h.NAMESPACE, "-o", "json", check=True).stdout
    igc: dict = json.loads(out)
    return igc


def _condition(obj: dict, type_: str) -> dict:
    return next((c for c in obj.get("status", {}).get("conditions", []) if c.get("type") == type_), {})


def _gateway_url(e2e: EndToEndDeployment) -> str:
    """In-cluster URL of the Envoy Service fronting the default InferenceGatewayConfig.

    The controller creates the Gateway (named after the IGC) in the ADD-ON namespace, and Envoy
    Gateway names the data-plane Service after a hash, so it is discovered by the
    owning-gateway labels rather than hardcoded. Its listener is plain HTTP :80 in-cluster
    (TLS is terminated at the load balancer the controller requests)."""
    gw_ns = h.jd_output(e2e, "hyperpod_inference_namespace")
    selector = h.jd_output(e2e, "hyperpod_inference_gateway_service_selector")
    out = run_kubectl(
        "get",
        "svc",
        "-n",
        gw_ns,
        "-l",
        selector,
        "-o",
        "json",
        check=True,
    ).stdout
    items = json.loads(out)["items"]
    assert items, f"no Envoy Service found in {gw_ns} for {selector}"
    svc = items[0]
    port = next(p["port"] for p in svc["spec"]["ports"] if p.get("protocol", "TCP") == "TCP")
    return f"http://{svc['metadata']['name']}.{svc['metadata']['namespace']}.svc:{port}"


def _prom_scalar(query: str) -> float:
    """Evaluate a PromQL instant query through the API-server service proxy; 0 if empty."""
    path = f"{PROMETHEUS_PROXY}/api/v1/query?query={urllib.parse.quote(query)}"
    body = json.loads(run_kubectl("get", "--raw", path, check=True).stdout)
    assert body["status"] == "success", body
    result = body["data"]["result"]
    return float(result[0]["value"][1]) if result else 0.0


def _send_chat_requests(e2e: EndToEndDeployment, url: str, model: str, count: int) -> list[dict]:
    """POST `count` chat completions to the gateway from one in-cluster client pod
    (resources/gateway-client.py on the python image) and return the parsed responses.

    The first request retries (vLLM cold start, EPP warm-up); the rest must each succeed."""
    script = (h.RESOURCES_DIR / "gateway-client.py").read_text()
    run_kubectl("delete", "pod", CLIENT_POD, "-n", h.NAMESPACE, "--ignore-not-found", check=False)
    run_kubectl(
        "run", CLIENT_POD, "-n", h.NAMESPACE, "--restart=Never", f"--image={h.python_image(e2e)}",
        f"--env=GATEWAY_URL={url}", f"--env=MODEL={model}", f"--env=COUNT={count}",
        # Pin to the system NG: attached HyperPod nodes are untainted but their instance role
        # cannot import ECR pull-through images, so an unpinned client may never pull.
        f"--overrides={json.dumps(SYSTEM_NG_OVERRIDES)}",
        "--", "python", "-c", script, check=True,
    )  # fmt: skip
    try:
        run_kubectl(
            "wait", "--for=jsonpath={.status.phase}=Succeeded", f"pod/{CLIENT_POD}", "-n", h.NAMESPACE,
            "--timeout=900s", check=False,
        )  # fmt: skip
        out = run_kubectl("logs", CLIENT_POD, "-n", h.NAMESPACE, check=True).stdout
    finally:
        run_kubectl("delete", "pod", CLIENT_POD, "-n", h.NAMESPACE, "--ignore-not-found", check=False)
    responses = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert len(responses) == count, f"expected {count} completions through the gateway, got:\n{out}"
    for r in responses:
        assert r["choices"][0]["message"]["content"].strip(), f"empty completion: {r!r}"
    return responses


@pytest.mark.full_deployment
@pytest.mark.mutating
def test_hyperpod_inference_addon_is_active(gateway: EndToEndDeployment) -> None:
    addon = _describe_addon(gateway)
    assert addon["status"] == "ACTIVE", f"add-on status {addon['status']}: {addon.get('health')}"
    assert not addon.get("health", {}).get("issues"), f"add-on health issues: {addon['health']['issues']}"
    config = json.loads(addon["configurationValues"])
    assert config["inferenceGateway"]["enabled"] is True and config["inferenceOperator"]["enabled"] is True
    namespace = h.jd_output(gateway, "hyperpod_inference_namespace")
    for deployment in ("hyperpod-inference-controller-manager", "inference-gateway-controller"):
        run_kubectl("rollout", "status", f"deployment/{deployment}", "-n", namespace, "--timeout=300s", check=True)
    run_kubectl("get", "crd", "inferencegatewayconfigs.inference.sagemaker.aws.amazon.com", check=True)


@pytest.mark.full_deployment
@pytest.mark.mutating
def _wait_for_conditions(e2e: EndToEndDeployment, types: tuple[str, ...], timeout_s: int = 600) -> dict:
    """Poll the IGC until every condition in `types` is True at the current generation."""
    deadline = time.monotonic() + timeout_s
    igc = _igc(e2e)
    while time.monotonic() < deadline:
        current = igc.get("status", {}).get("observedGeneration") == igc["metadata"]["generation"]
        if current and all(_condition(igc, t).get("status") == "True" for t in types):
            break
        time.sleep(10)
        igc = _igc(e2e)
    for t in types:
        assert _condition(igc, t).get("status") == "True", (
            f"IGC condition {t} not True: {json.dumps(igc.get('status'))}"
        )
    return igc


@pytest.mark.full_deployment
@pytest.mark.mutating
def test_default_inference_gateway_config_is_ready(gateway: EndToEndDeployment) -> None:
    """The platform half of the gateway is up with no model attached yet: the controller
    accepted the IGC, programmed the Gateway (Envoy + its internal NLB) and rolled out the BBR,
    one EPP Deployment + InferencePool per scheduler, and the HTTPS ALB Ingress. The overall
    `Ready` condition additionally needs model-server pods (BackendsNotReady until then), so
    it is asserted by the serving test once a model is attached."""
    igc = _wait_for_conditions(gateway, ("Accepted", "GatewayProgrammed", "BBRReady"))
    gw_ns = h.jd_output(gateway, "hyperpod_inference_namespace")
    label_key = h.jd_output(gateway, "hyperpod_inference_gateway_scheduler_label")
    assert igc["spec"]["bbr"]["enabled"] is True
    assert igc["spec"]["schedulers"], "the default gateway must declare at least one scheduler"
    for sched in igc["spec"]["schedulers"]:
        assert sched["modelSelector"]["matchLabels"] == {label_key: sched["name"]}, sched
        run_kubectl("get", "inferencepool", sched["name"], "-n", h.NAMESPACE, check=True)
        epps = run_kubectl(
            "get", "deploy", "-n", h.NAMESPACE, "-l", f"inference.sagemaker.aws.amazon.com/scheduler={sched['name']}",
            "-o", "jsonpath={.items[*].metadata.name}", check=True,
        ).stdout.split()  # fmt: skip
        assert epps, f"no EPP Deployment for scheduler {sched['name']}"
        for epp in epps:
            run_kubectl("rollout", "status", f"deployment/{epp}", "-n", h.NAMESPACE, "--timeout=300s", check=True)
    assert _gateway_url(gateway).startswith("http://")
    # The external (in-VPC) HTTPS endpoint: an internal ALB Ingress fronted by the
    # auto-issued ACM certificate.
    name = h.jd_output(gateway, "hyperpod_inference_gateway_config_name")
    ingress = json.loads(run_kubectl("get", "ingress", f"{name}-alb", "-n", gw_ns, "-o", "json", check=True).stdout)
    annotations = ingress["metadata"]["annotations"]
    assert annotations.get("alb.ingress.kubernetes.io/scheme") == "internal", annotations
    assert annotations.get("alb.ingress.kubernetes.io/certificate-arn", "").startswith("arn:aws:acm:"), annotations


@pytest.mark.gpu
@pytest.mark.full_deployment
@pytest.mark.mutating
def test_requests_through_gateway_increment_pool_metrics(gateway: EndToEndDeployment) -> None:
    region = h.jd_output(gateway, "region")
    igc = _igc(gateway)
    scheduler = igc["spec"]["schedulers"][0]
    model, label_key = scheduler["modelName"], h.jd_output(gateway, "hyperpod_inference_gateway_scheduler_label")

    overrides = h.onboard_chart(gateway, region, "vllm-qwen")
    try:
        # Serve under the scheduler's model name and join its InferencePool via the label.
        subprocess.run(
            [
                "helm", "install", RELEASE, str(h.CHARTS_DIR / "vllm-qwen"), "-n", h.NAMESPACE,
                "-f", str(overrides),
                "--set", f"servedModelName={model}",
                "--set-json", f"podLabels={json.dumps({label_key: scheduler['name']})}",
            ],
            check=True, capture_output=True, text=True,
        )  # fmt: skip
        rollout = run_kubectl(
            "rollout", "status", f"deployment/{RELEASE}", "-n", h.NAMESPACE, "--timeout=1200s", check=False
        )
        assert rollout.returncode == 0, f"vLLM rollout failed: {rollout.stderr}"
        # With a labeled model pod in its pool, the IGC turns fully Ready.
        _wait_for_conditions(gateway, ("Ready",))

        selector = f'{{model_name="{model}"}}'
        before = {m: _prom_scalar(f"sum({m}{selector})") for m in POOL_REQUEST_METRICS}
        before[BBR_SUCCESS_METRIC] = _prom_scalar(f"sum({BBR_SUCCESS_METRIC})")

        _send_chat_requests(gateway, _gateway_url(gateway), model, REQUESTS_SENT)

        # Scrape interval 15s: poll until every counter reflects the requests.
        deadline = time.monotonic() + 180
        after = before
        while time.monotonic() < deadline:
            after = {m: _prom_scalar(f"sum({m}{selector})") for m in POOL_REQUEST_METRICS}
            after[BBR_SUCCESS_METRIC] = _prom_scalar(f"sum({BBR_SUCCESS_METRIC})")
            if all(after[m] - before[m] >= REQUESTS_SENT for m in after):
                break
            time.sleep(15)
        for m in after:
            assert after[m] - before[m] >= REQUESTS_SENT, (
                f"{m}: {before[m]} -> {after[m]} after {REQUESTS_SENT} requests"
            )
    finally:
        subprocess.run(["helm", "uninstall", RELEASE, "-n", h.NAMESPACE], check=False, capture_output=True)
