"""Node-level helpers for the GPU parallel-pull benchmark.

Colocated with their only consumer (test_gpu_parallel_pull_bench.py). These run node-level
commands via `kubectl debug node` and roll a node's containerd config in place — things the
pass/fail e2e suite never does. Not promoted to a shared module: there is exactly one consumer
today; promote if/when a second test actually needs them.
"""

import re

from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl


def _node_debug(node: str, image: str, script: str) -> str:
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


def containerd_config_dump(node: str, image: str) -> str:
    """`containerd config dump` on the node — containerd's effective, merged config."""
    return _node_debug(node, image, "containerd config dump 2>/dev/null")


def crictl_pull_seconds(node: str, image: str, ref: str, *, remove_first: bool = True) -> float:
    """Time a `crictl pull` of `ref` on the node (wall seconds); -1.0 if the pull failed.

    remove_first=True does `crictl rmi` before pulling to force a fresh pull (measure the
    download+unpack path, not a cache hit). Pass False for a warmup that just populates caches.
    """
    rmi = f"crictl rmi {ref} >/dev/null 2>&1; " if remove_first else ""
    script = (
        f"{rmi}"
        f"S=$(date +%s.%N); "
        f"crictl pull {ref} >/dev/null 2>&1 && E=$(date +%s.%N) && "
        f"awk -v s=$S -v e=$E 'BEGIN{{printf \"PULL_OK %.3f\\n\", e - s}}'"
    )
    out = _node_debug(node, image, script)
    m = re.search(r"PULL_OK\s+([\d.]+)", out)
    return float(m.group(1)) if m else -1.0


def write_containerd_dropin(node: str, image: str, path: str, toml_body: str) -> None:
    """Write a containerd config drop-in on the node (via a quoted heredoc) and restart containerd.

    A quoted heredoc (<<'EOF') writes `toml_body` VERBATIM — critical because containerd table
    paths contain single quotes (e.g. [plugins.'io.containerd.transfer.v1.local']); a
    single-quoted printf would terminate early and silently corrupt the table into a dotted key.
    `path` is under /etc/containerd/config.d (AL2023 nodeadm's merge dir).
    """
    script = (
        f'mkdir -p "$(dirname {path})" && '
        f"cat > {path} <<'CONTAINERD_DROPIN_EOF'\n"
        f"{toml_body}\n"
        "CONTAINERD_DROPIN_EOF\n"
        "systemctl restart containerd && sleep 3 && echo DROPIN_APPLIED"
    )
    out = _node_debug(node, image, script)
    assert "DROPIN_APPLIED" in out, f"failed to write containerd drop-in {path} on {node}:\n{out}"


def remove_containerd_dropin(node: str, image: str, path: str) -> None:
    """Remove a containerd config drop-in and restart containerd (restore booted state)."""
    _node_debug(node, image, f"rm -f {path}; systemctl restart containerd 2>/dev/null; true")


def wait_pod_ready(namespace: str, pod: str, timeout_s: int = 600) -> bool:
    """Poll until the pod is Ready (its node provisioned + probe image pulled)."""
    res = run_kubectl(
        "wait", f"pod/{pod}", "-n", namespace, "--for=condition=Ready", f"--timeout={timeout_s}s", check=False
    )
    return res.returncode == 0
