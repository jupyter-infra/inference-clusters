"""Node-level helpers for the GPU parallel-pull benchmark.

Read the effective containerd config and evict images on a node's host via `kubectl debug node`.
"""

import re

from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl

TRANSFER_PLUGIN = "io.containerd.transfer.v1.local"
CRI_IMAGES_PLUGIN = "io.containerd.cri.v1.images"
# conf.d drop-in the benchmark writes to flip the node to local-pull mode for the OFF measurement.
OFF_DROPIN = "/etc/containerd/conf.d/99-bench-off.toml"


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


def uses_transfer_service(node: str, image: str) -> bool:
    """Whether pod pulls use the transfer service (not the local-pull fallback).

    EKS defaults discard_unpacked_layers=true, which forces local pull mode; the feature sets it
    false. containerd's `config dump` reflects the effective value, and it logs a fallback warning
    when it reverts — treat local mode as transfer-service-off.
    """
    dump = containerd_config_dump(node, image)
    m = re.search(r"discard_unpacked_layers\s*=\s*(true|false)", dump)
    return m is not None and m.group(1) == "false"


def transfer_max_downloads(node: str, image: str) -> int:
    """max_concurrent_downloads under the transfer plugin in the effective config (-1 if absent)."""
    dump = containerd_config_dump(node, image)
    m = re.search(
        rf"\[plugins\.['\"]?{re.escape(TRANSFER_PLUGIN)}['\"]?\].*?max_concurrent_downloads\s*=\s*(\d+)",
        dump,
        re.DOTALL,
    )
    return int(m.group(1)) if m else -1


def set_local_pull_fallback(node: str, image: str) -> None:
    """Flip the node back to EKS-default local pull mode in place, then restart containerd.

    Writes a conf.d drop-in setting discard_unpacked_layers=true (the default the feature
    disables), which forces local pull mode and its lower concurrency. Same node, same hardware —
    only the pull path changes. containerd imports /etc/containerd/conf.d/*.toml on restart.
    """
    script = (
        f"cat > {OFF_DROPIN} <<'EOF'\n"
        f"[plugins.'{CRI_IMAGES_PLUGIN}']\n"
        "discard_unpacked_layers = true\n"
        "EOF\n"
        "systemctl restart containerd && sleep 4 && echo FLIP_DONE"
    )
    out = _node_debug(node, image, script)
    assert "FLIP_DONE" in out, f"failed to apply local-pull drop-in on {node}:\n{out}"


def clear_pull_override(node: str, image: str) -> None:
    """Remove the benchmark drop-in and restart containerd (restore the booted config)."""
    _node_debug(node, image, f"rm -f {OFF_DROPIN}; systemctl restart containerd 2>/dev/null; true")


def evict_image(node: str, image: str, ref: str) -> None:
    """Remove `ref` and prune its layer blobs so the next pull is a true cold pull.

    `images rm` only drops the reference; `content prune references` evicts the unreferenced
    blobs (without it a re-pull is a warm no-op).
    """
    _node_debug(
        node,
        image,
        f"ctr -n k8s.io images rm {ref} >/dev/null 2>&1; ctr -n k8s.io content prune references >/dev/null 2>&1; true",
    )


def wait_pod_ready(namespace: str, pod: str, timeout_s: int = 600) -> bool:
    """Poll until the pod is Ready (its node provisioned + probe image pulled)."""
    res = run_kubectl(
        "wait", f"pod/{pod}", "-n", namespace, "--for=condition=Ready", f"--timeout={timeout_s}s", check=False
    )
    return res.returncode == 0
