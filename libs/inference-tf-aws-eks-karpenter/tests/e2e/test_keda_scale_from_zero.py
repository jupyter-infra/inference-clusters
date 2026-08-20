"""Gated live E2E — KEDA online scale-from-zero via a router/activator.

Online serving autoscaling has a chicken-and-egg: you cannot scale a serving Deployment
0->1 on a metric it emits itself (at 0 replicas there is no pod and no metric series).
This test uses the Knative-activator pattern to break it:

  1. onboard the vLLM chart; install it SCALED TO ZERO (replicas=0);
  2. deploy an ALWAYS-ON router (1 replica) that proxies /v1/chat/completions to the vLLM
     Service and exposes `router_inflight_requests` — a metric that EXISTS at zero serving
     replicas because the router is always up (scraped via a release-labelled ServiceMonitor);
  3. apply a KEDA ScaledObject that scales the vLLM pod 0->1 on `router_inflight_requests > 0`;
  4. a busybox client POSTs through the router. The router holds the request and retries
     the backend while KEDA scales vLLM 0->1 -> the pod bin-packs onto the warm g node
     (the router is pinned to gpu-g, so a node is already up — this isolates the
     router-metric -> KEDA -> pod-scale half of the chain; node provisioning is covered by
     test_vllm_serving / test_kro_graph_serving) -> vLLM serves -> the completion returns;
  5. the request completes -> `router_inflight_requests` drops to 0 -> KEDA scales vLLM to 0.

Marked `full_deployment` (real GPU + ~15GB weights + cold start) — runs only with
full-deploy=true. Charts/manifests are fixtures, never shipped in the template.
"""

import subprocess
import time

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl

from tests.e2e import _serving_helpers as h

CHART = h.CHARTS_DIR / "vllm-qwen"
RELEASE = "vllm-keda-e2e"  # the vLLM serving Deployment KEDA scales
ROUTER_SCRIPT = h.RESOURCES_DIR / "router.py"
ROUTER_CM = "keda-router-script"


def _replicas(release: str) -> int:
    r = run_kubectl("get", "deployment", release, "-n", h.NAMESPACE, "-o", "jsonpath={.spec.replicas}", check=False)
    return int(r.stdout.strip() or "0")


def _wait_settled_at_zero(release: str, *, stable_reads: int = 3, interval_s: int = 5, timeout_s: int = 180) -> None:
    """Block until the ScaledObject holds vLLM at its 0 floor, then assert it's there.

    Replaces a blind sleep: the at-rest state is established by an OBSERVED condition,
    not a fixed guess at KEDA's reconcile latency. Two gates:
      1. `kubectl wait --for=condition=Ready` — KEDA has reconciled the ScaledObject and
         created the backing HPA (until then replicas may still read the install-time 0
         for the wrong reason: nothing is managing it yet).
      2. a stable window — `stable_reads` consecutive zero reads, so a still-settling
         count that momentarily reads 0 does not pass prematurely.
    """
    ready = run_kubectl(
        "wait",
        "--for=condition=Ready",
        f"scaledobject/{release}",
        "-n",
        h.NAMESPACE,
        f"--timeout={timeout_s}s",
        check=False,
    )
    assert ready.returncode == 0, f"ScaledObject '{release}' never became Ready:\n{ready.stderr}"

    deadline = time.monotonic() + timeout_s
    consecutive = 0
    while time.monotonic() < deadline:
        if _replicas(release) == 0:
            consecutive += 1
            if consecutive >= stable_reads:
                return
        else:
            consecutive = 0
        time.sleep(interval_s)
    raise AssertionError(
        f"vLLM did not settle at 0 replicas ({stable_reads} stable reads) within {timeout_s}s "
        "before any request drove the router metric"
    )


@pytest.mark.gpu
@pytest.mark.full_deployment
def test_keda_scales_vllm_from_zero_via_router(e2e_deployment: EndToEndDeployment) -> None:
    e2e_deployment.ensure_deployed()
    region = h.jd_output(e2e_deployment, "region")
    e2e_deployment.cli.run_command(["jupyter-deploy", "cluster", "login"])

    overrides = h.onboard_chart(e2e_deployment, region, "vllm-qwen")
    assert h.WORKLOAD_IMAGE_SUFFIX in overrides.read_text()

    try:
        # 1. Install vLLM scaled-to-zero. KEDA (below) owns the replica count from here.
        subprocess.run(
            ["helm", "install", RELEASE, str(CHART), "-n", h.NAMESPACE, "-f", str(overrides), "--set", "replicas=0"],
            check=True,
            capture_output=True,
            text=True,
        )

        # 2. Deploy the always-on router. Its script is delivered via a ConfigMap built
        #    from router.py (the file stays the single source of truth), and it targets
        #    the vLLM Service. Pinning it to gpu-g keeps a node warm for the scale event.
        run_kubectl(
            "create", "configmap", ROUTER_CM, "-n", h.NAMESPACE, f"--from-file=router.py={ROUTER_SCRIPT}", check=True
        )
        h.apply_resource(
            "router.yaml",
            router_image=h.python_image(e2e_deployment),
            backend_url=f"http://{RELEASE}.{h.NAMESPACE}.svc:8000",
        )
        run_kubectl("rollout", "status", "deployment/keda-router", "-n", h.NAMESPACE, "--timeout=300s", check=True)

        # 3. Apply the ScaledObject (minReplicaCount=0, scales on router_inflight_requests).
        h.apply_resource("vllm-scaledobject.yaml", target=RELEASE)

        # 3a. At rest (no requests): KEDA holds vLLM at 0, no serving pod. Wait on the
        #     ScaledObject reconciling + a stable-at-zero window rather than a blind sleep.
        _wait_settled_at_zero(RELEASE)

        # 4. Fire a request through the router (soft-fail + retry; the router holds it and
        #    retries the backend through the cold start). Returns immediately — the client
        #    keeps running so we can watch vLLM scale WHILE the request is in flight.
        h.launch_blocking_invoke(e2e_deployment, "keda-router", port=8080)

        # The held request drives router_inflight_requests -> KEDA activates -> vLLM 0->1.
        scaled_up = False
        for _ in range(16):  # ~4 min for router scrape + KEDA poll + HPA to raise replicas
            if _replicas(RELEASE) >= 1:
                scaled_up = True
                break
            time.sleep(15)
        assert scaled_up, "KEDA must scale vLLM 0->1 once a held request pushes router_inflight_requests over threshold"

        # The scaled-up pod bin-packs onto the warm g node; vLLM loads weights + serves.
        rollout = run_kubectl(
            "rollout", "status", f"deployment/{RELEASE}", "-n", h.NAMESPACE, "--timeout=1200s", check=False
        )
        if rollout.returncode != 0:
            desc = run_kubectl("describe", "pods", "-n", h.NAMESPACE, "-l", f"app={RELEASE}", check=False).stdout
            raise AssertionError(f"scaled-up vLLM did not become ready:\n{rollout.stderr}\n{desc[-2000:]}")
        h.assert_on_karpenter_gpu(RELEASE)

        # The router's held request now completes end-to-end with a real completion.
        h.collect_blocking_invoke()

        # 5. Request done -> router_inflight_requests drops to 0 -> KEDA scales vLLM to 0.
        scaled_down = False
        for _ in range(16):  # cooldown (30s) + poll + KEDA scale-to-zero
            if _replicas(RELEASE) == 0:
                scaled_down = True
                break
            time.sleep(15)
        assert scaled_down, "KEDA must scale vLLM back to 0 after the in-flight metric drops to 0"
    finally:
        # Delete by kind/name (router.yaml carries ${...} placeholders kubectl can't parse).
        run_kubectl("delete", "pod", "keda-client", "-n", h.NAMESPACE, "--ignore-not-found", check=False)
        run_kubectl("delete", "scaledobject", RELEASE, "-n", h.NAMESPACE, "--ignore-not-found", check=False)
        run_kubectl("delete", "servicemonitor", "keda-router", "-n", h.NAMESPACE, "--ignore-not-found", check=False)
        run_kubectl("delete", "service", "keda-router", "-n", h.NAMESPACE, "--ignore-not-found", check=False)
        run_kubectl("delete", "deployment", "keda-router", "-n", h.NAMESPACE, "--ignore-not-found", check=False)
        run_kubectl("delete", "configmap", ROUTER_CM, "-n", h.NAMESPACE, "--ignore-not-found", check=False)
        subprocess.run(["helm", "uninstall", RELEASE, "-n", h.NAMESPACE], check=False, capture_output=True)
