"""Node-level helpers for the GPU SOCI parallel-pull benchmark.

Read the effective containerd config and evict images on a node's host via `kubectl debug node`.
"""

import re

from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl

CRI_IMAGES_PLUGIN = "io.containerd.cri.v1.images"


def _node_debug(node: str, image: str, script: str) -> str:
    """Run a shell script on a node's host root via `kubectl debug node/<n>` (chroot /host).

    --attach is required: without it `kubectl debug node/` returns only the "Creating..." notice
    and the command's stdout is never captured.
    """
    res = run_kubectl(
        "debug",
        f"node/{node}",
        "-q",
        "--attach",
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


def uses_soci_snapshotter(node: str, image: str) -> bool:
    """Whether containerd's CRI images plugin is set to the SOCI snapshotter (parallel pull/unpack).

    nodeadm's FastImagePull gate sets snapshotter = "soci" under the CRI images plugin; that is
    what the node's effective `containerd config dump` shows when the feature took effect.
    """
    dump = containerd_config_dump(node, image)
    m = re.search(
        rf"\[plugins\.['\"]?{re.escape(CRI_IMAGES_PLUGIN)}['\"]?\].*?snapshotter\s*=\s*['\"]soci['\"]",
        dump,
        re.DOTALL,
    )
    return m is not None


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
