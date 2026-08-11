"""Node-level helpers specific to the GPU SOCI parallel-pull benchmark.

Node-host access is the shared `_serving_helpers.node_shell`; this module adds only the
benchmark's own concerns — detecting the SOCI snapshotter and evicting an image for a cold pull.
"""

import re

from pytest_jupyter_deploy.kubernetes.kubectl import run_kubectl

from tests.e2e import _serving_helpers as h

# containerd's CRI images plugin table (config schema v3). nodeadm's FastImagePull gate sets
# its snapshotter to "soci" (parallel pull/unpack) — the on-node effect the benchmark verifies.
CRI_IMAGES_PLUGIN = "io.containerd.cri.v1.images"


def uses_soci_snapshotter(node: str, image: str) -> bool:
    """Whether the node's effective containerd config sets the CRI images snapshotter to "soci".

    Scopes the match to the CRI images plugin table so an unrelated soci mention can't false-pass.
    """
    dump = h.node_shell(node, image, "containerd config dump 2>/dev/null")
    return (
        re.search(
            rf"\[plugins\.['\"]?{re.escape(CRI_IMAGES_PLUGIN)}['\"]?\].*?snapshotter\s*=\s*['\"]soci['\"]",
            dump,
            re.DOTALL,
        )
        is not None
    )


def evict_image(node: str, image: str, ref: str) -> None:
    """Remove `ref` and prune its layer blobs so the next pull is a true cold pull.

    `images rm` only drops the reference; `content prune references` evicts the unreferenced
    blobs (without it a re-pull is a warm no-op).
    """
    h.node_shell(
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
