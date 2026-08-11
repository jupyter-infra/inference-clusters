"""Helpers for the GPU parallel-pull benchmark (tests/load).

Kept separate from the e2e helpers: these run node-level containerd/crictl commands via
`kubectl debug node` and mutate a node's containerd config in place, which the pass/fail
e2e suite never does.

Design — same-instance rolling comparison: to keep instance type / AZ / EBS / NIC out of
the measurement, BOTH the on and off pulls happen on the SAME real `gpu` node. We measure
the cold pull with parallel-pull ON (the default), then flip the transfer-plugin config OFF
on that node in place, restart containerd, and re-measure the same image. The only variable
between the two numbers is the config.
"""

import re
import time

from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl

# The three keys the feature sets under the transfer plugin table.
TRANSFER_KEYS = ("max_concurrent_downloads", "concurrent_layer_fetch_buffer", "max_concurrent_unpacks")
TRANSFER_PLUGIN = "io.containerd.transfer.v1.local"
# Drop-in the benchmark writes to flip the node's config (AL2023 nodeadm merge dir).
BENCH_DROPIN = "/etc/containerd/config.d/99-bench-parallel-pull.toml"


def node_debug(node: str, image: str, script: str) -> str:
    """Run a shell script on a node's host root via `kubectl debug node/<n>` (chroot /host)."""
    res = run_kubectl(
        "debug",
        f"node/{node}",
        "-q",
        f"--image={image}",
        "--",
        "chroot",
        "/host",
        "sh",
        "-c",
        script,
        check=False,
    )
    return res.stdout


def config_dump(node: str, image: str) -> str:
    """`containerd config dump` on the node — containerd's effective, merged config."""
    return node_debug(node, image, "containerd config dump 2>/dev/null")


def assert_transfer_keys_active(node: str, image: str) -> dict[str, int]:
    """Assert the three transfer keys are present under the ACTIVE io.containerd.transfer.v1.local
    table in `containerd config dump`, and return the parsed key->value map.

    The correctness check the PR review asked for: config dump is the effective merged config, so
    a present key proves it landed under the active plugin (not just written to a file).
    """
    dump = config_dump(node, image)
    assert TRANSFER_PLUGIN in dump, (
        f"{node}: '{TRANSFER_PLUGIN}' absent from `containerd config dump` — parallel-pull keys "
        f"are inert on this containerd build.\n--- dump tail ---\n{dump[-1500:]}"
    )
    found: dict[str, int] = {}
    for key in TRANSFER_KEYS:
        m = re.search(rf"^\s*{re.escape(key)}\s*=\s*(\d+)", dump, re.MULTILINE)
        if m:
            found[key] = int(m.group(1))
    return found


def cri_uses_transfer_service(node: str, image: str) -> bool:
    """Best-effort: does CRI route image pulls through the transfer service?

    The PR review's second concern — even with the keys active, the speedup only applies if
    kubelet/CRI pulls go through the transfer service (not the legacy CRI puller). containerd 2.x
    exposes this as the CRI `use_local_image_pull` / `image_pull_with_sync` path. Informational.
    """
    dump = config_dump(node, image)
    return "use_local_image_pull = true" in dump or "image_pull_with_sync = true" in dump


def crictl_cold_pull_seconds(node: str, image: str, ref: str) -> float:
    """Remove `ref` from the node then time a fresh `crictl pull` (wall seconds).

    Cold pull only: rmi first so we measure the download+unpack path, not a cache hit.
    Returns -1.0 if the pull failed.
    """
    script = (
        f"crictl rmi {ref} >/dev/null 2>&1; "
        f"S=$(date +%s.%N); "
        f"crictl pull {ref} >/dev/null 2>&1 && E=$(date +%s.%N) && "
        f"awk -v s=$S -v e=$E 'BEGIN{{printf \"PULL_OK %.3f\\n\", e - s}}'"
    )
    out = node_debug(node, image, script)
    m = re.search(r"PULL_OK\s+([\d.]+)", out)
    return float(m.group(1)) if m else -1.0


def set_parallel_pull(node: str, image: str, enabled: bool) -> None:
    """Rewrite the node's containerd transfer-plugin config IN PLACE, then restart containerd.

    Rolling the SAME instance between the on and off measurements (no node replacement), so
    instance type/AZ/EBS/NIC are held constant. enabled=True writes the feature values;
    enabled=False forces concurrency to 1 (feature effectively off, same-shaped config).
    """
    downloads, unpacks, buffer = (20, 5, 16777216) if enabled else (1, 1, 0)
    block = (
        "[plugins.'io.containerd.transfer.v1.local']\\n"
        f"max_concurrent_downloads = {downloads}\\n"
        f"concurrent_layer_fetch_buffer = {buffer}\\n"
        f"max_concurrent_unpacks = {unpacks}\\n"
    )
    script = (
        "mkdir -p /etc/containerd/config.d && "
        f"printf '{block}' > {BENCH_DROPIN} && "
        "systemctl restart containerd && sleep 3 && echo BENCH_APPLIED"
    )
    out = node_debug(node, image, script)
    assert "BENCH_APPLIED" in out, f"failed to apply parallel-pull={enabled} on {node}:\n{out}"


def clear_bench_override(node: str, image: str) -> None:
    """Remove the benchmark drop-in and restart containerd (restore node to its booted state)."""
    node_debug(node, image, f"rm -f {BENCH_DROPIN}; systemctl restart containerd 2>/dev/null; true")


def wait_pod_ready(namespace: str, pod: str, timeout_s: int = 600) -> bool:
    """Poll until the pod is Ready (its node provisioned + probe image pulled)."""
    res = run_kubectl(
        "wait", f"pod/{pod}", "-n", namespace, "--for=condition=Ready", f"--timeout={timeout_s}s", check=False
    )
    return res.returncode == 0


def node_of_pod(namespace: str, pod: str) -> str:
    """The node a pod landed on."""
    for _ in range(30):
        node = run_kubectl(
            "get", "pod", pod, "-n", namespace, "-o", "jsonpath={.spec.nodeName}", check=False
        ).stdout.strip()
        if node:
            return node
        time.sleep(2)
    return ""
