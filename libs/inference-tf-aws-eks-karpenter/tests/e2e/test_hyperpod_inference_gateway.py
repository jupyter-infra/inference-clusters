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

# The gateway's REAL metric names, as observed in the live Prometheus (Gateway API Inference
# Extension EPP metrics, labeled per model / per InferencePool). Each MUST grow by at least
# REQUESTS_SENT after the requests go through the gateway.
POOL_REQUEST_METRICS = (
    "inference_objective_request_total",
    "inference_objective_request_duration_seconds_count",
    "inference_objective_input_tokens_count",
    "inference_objective_output_tokens_count",
)


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

    Envoy Gateway names the data-plane Service after a hash, so it is discovered by the
    owning-gateway labels rather than hardcoded."""
    name = h.jd_output(e2e, "hyperpod_inference_gateway_config_name")
    out = run_kubectl(
        "get",
        "svc",
        "-A",
        "-l",
        f"gateway.envoyproxy.io/owning-gateway-name={name},gateway.envoyproxy.io/owning-gateway-namespace={h.NAMESPACE}",
        "-o",
        "json",
        check=True,
    ).stdout
    items = json.loads(out)["items"]
    assert items, f"no Envoy Service found for gateway {h.NAMESPACE}/{name}"
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
    """POST `count` chat completions to the gateway from one in-cluster client pod.

    The first request retries (vLLM cold start, EPP warm-up); the rest are single shots and
    must each return a completion. Returns the parsed responses."""
    prompt = h._chat_prompt(model)
    first = h._retry_invoke_script(f"{url}/v1/chat/completions", prompt, attempts=30, read_timeout_s=300, backoff_s=8)
    rest = (
        f"for i in $(seq 2 {count}); do "
        f"  wget -q -O- -T 300 --post-data='{prompt}' --header='Content-Type: application/json' "
        f"    {url}/v1/chat/completions || exit 1; echo; "
        "done"
    )
    script = f"{first.replace('exit 0', 'break')}; {rest}"
    run_kubectl("delete", "pod", CLIENT_POD, "-n", h.NAMESPACE, "--ignore-not-found", check=False)
    run_kubectl(
        "run", CLIENT_POD, "-n", h.NAMESPACE, "--restart=Never", f"--image={h.client_image(e2e)}", "--",
        "sh", "-c", script, check=True,
    )  # fmt: skip
    try:
        run_kubectl(
            "wait", "--for=jsonpath={.status.phase}=Succeeded", f"pod/{CLIENT_POD}", "-n", h.NAMESPACE,
            "--timeout=900s", check=True,
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
def test_default_inference_gateway_config_is_ready(gateway: EndToEndDeployment) -> None:
    deadline = time.monotonic() + 600
    igc = _igc(gateway)
    while _condition(igc, "Ready").get("status") != "True" and time.monotonic() < deadline:
        time.sleep(10)
        igc = _igc(gateway)
    assert _condition(igc, "Ready").get("status") == "True", f"IGC not Ready: {json.dumps(igc.get('status'))}"
    assert igc["spec"]["bbr"]["enabled"] is True
    label_key = h.jd_output(gateway, "hyperpod_inference_gateway_scheduler_label")
    assert igc["spec"]["schedulers"], "the default gateway must declare at least one scheduler"
    for sched in igc["spec"]["schedulers"]:
        assert sched["modelSelector"]["matchLabels"] == {label_key: sched["name"]}, sched
    # The data plane exists: an Envoy Service fronts the gateway.
    assert _gateway_url(gateway).startswith("http://")


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

        selector = f'{{model_name="{model}"}}'
        before = {m: _prom_scalar(f"sum({m}{selector})") for m in POOL_REQUEST_METRICS}

        _send_chat_requests(gateway, _gateway_url(gateway), model, REQUESTS_SENT)

        # Scrape interval 15s: poll until every counter reflects the requests.
        deadline = time.monotonic() + 180
        after = before
        while time.monotonic() < deadline:
            after = {m: _prom_scalar(f"sum({m}{selector})") for m in POOL_REQUEST_METRICS}
            if all(after[m] - before[m] >= REQUESTS_SENT for m in POOL_REQUEST_METRICS):
                break
            time.sleep(15)
        for m in POOL_REQUEST_METRICS:
            assert after[m] - before[m] >= REQUESTS_SENT, (
                f"{m}: {before[m]} -> {after[m]} after {REQUESTS_SENT} requests"
            )
    finally:
        subprocess.run(["helm", "uninstall", RELEASE, "-n", h.NAMESPACE], check=False, capture_output=True)
