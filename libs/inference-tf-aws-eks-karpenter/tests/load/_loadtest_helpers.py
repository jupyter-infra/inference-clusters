"""Node-level + parsing helpers for the load/benchmark suite.

Kept out of the test body so the benchmark reads as orchestration only (deploy → measure →
report). Node-host access itself is the shared `_serving_helpers.node_shell`.
"""

import re
import string
import subprocess
from pathlib import Path

from tests.e2e import _serving_helpers as h

RESOURCES_DIR = Path(__file__).resolve().parent / "resources"

# containerd's CRI images plugin table (config schema v3). nodeadm's FastImagePull gate sets its
# snapshotter to "soci" (parallel pull/unpack) — the on-node effect the benchmark verifies.
CRI_IMAGES_PLUGIN = "io.containerd.cri.v1.images"


def apply_load_resource(name: str, **subs: str) -> str:
    """kubectl-apply a manifest from tests/load/resources/, substituting any ${...} vars.

    Mirrors tests/e2e `apply_resource` so load-test YAML lives in files, not test-body heredocs.
    """
    text = (RESOURCES_DIR / name).read_text()
    if subs:
        text = string.Template(text).substitute(**subs)
    subprocess.run(["kubectl", "apply", "-f", "-"], input=text, text=True, check=True, capture_output=True)
    return text


def uses_soci_snapshotter(node: str, image: str) -> bool:
    """Whether the node's effective containerd config sets the CRI images snapshotter to "soci".

    Scopes the match to the CRI images plugin table so an unrelated soci mention can't false-pass.
    """
    dump = h.node_shell(node, image, "containerd config dump 2>/dev/null")
    # (?:(?!\n\[)[\s\S])*? spans lines but stops at the next "[..." table header, so a
    # snapshotter="soci" in a LATER plugin table can't satisfy the CRI-images match.
    return (
        re.search(
            rf"\[plugins\.['\"]?{re.escape(CRI_IMAGES_PLUGIN)}['\"]?\](?:(?!\n\[)[\s\S])*?"
            r"snapshotter\s*=\s*['\"]soci['\"]",
            dump,
        )
        is not None
    )


def evict_image(node: str, image: str, ref: str) -> None:
    """Evict `ref` so the next pull is a true cold download + unpack.

    Under the SOCI snapshotter the unpacked layers live as soci snapshots on disk; `images rm`
    alone leaves them, so a re-pull is a warm no-op. Dropping the image, removing its soci
    snapshots (leaf-first), and restarting the snapshotter releases them for a genuine cold pull.
    """
    script = (
        f"ctr -n k8s.io images rm {ref} >/dev/null 2>&1; "
        'for k in $(ctr -n k8s.io snapshot --snapshotter soci ls 2>/dev/null | awk "NR>1{print \\$1}" | tac); do '
        'ctr -n k8s.io snapshot --snapshotter soci rm "$k" >/dev/null 2>&1; done; '
        "systemctl restart soci-snapshotter.service >/dev/null 2>&1; sleep 3; true"
    )
    h.node_shell(node, image, script)


def parse_pull_duration(msg: str) -> float | None:
    """Parse kubelet's 'Successfully pulled image ... in 3m31.7s' into seconds; None if absent."""
    m = re.search(r"in ((?:\d+m)?[\d.]+m?s)", msg)
    if not m:
        return None
    text = m.group(1)
    mins = re.search(r"(\d+)m(?!s)", text)
    secs = re.search(r"([\d.]+)s", text)
    total = (int(mins.group(1)) * 60 if mins else 0) + (float(secs.group(1)) if secs else 0.0)
    return total or None
