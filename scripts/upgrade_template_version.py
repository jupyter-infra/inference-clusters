#!/usr/bin/env python3
"""Bump the version across every file that pins it, for one template in this monorepo.

Usage:
    python scripts/upgrade_template_version.py <template> <bump-or-version>

    <template>         a key from TEMPLATES below (e.g. eks-karpenter)
    <bump-or-version>  patch | minor | major | an explicit version (e.g. 0.1.0, 0.1.0rc2)

Templates in this monorepo are **independently versioned** — each is a separately published
PyPI package — so a bump targets exactly one template. Register a new template by adding a
`TemplateSpec` to TEMPLATES; nothing else changes.

For each template, the version is pinned in several files that MUST stay in sync (the first
four are enforced byte-equal by that template's tests/unit/test_version.py):
    - pyproject.toml                    project.version         (PEP 440, verbatim)
    - <module>/__init__.py              __version__             (PEP 440, verbatim)
    - template/manifest.yaml            template.version        (PEP 440, verbatim)
    - template/engine/main.tf           template_version local  (PEP 440, verbatim)
    - template/charts/<c>/Chart.yaml    version                 (SemVer, undotted pre-release)

The synced Helm charts require SemVer, and this repo spells the pre-release WITHOUT a dot
(`0.1.0rc1` -> `0.1.0-rc1`). Charts NOT listed in `synced_charts` (e.g. karpenter's `kueue`,
which carries an independent version) are left untouched.

For a `patch|minor|major` bump the new version is computed from the target's current
pyproject.toml version, using only its X.Y.Z core (any pre-release suffix is dropped). Pass an
explicit version to cut a pre-release (e.g. `0.1.0rc2`).

This script does NOT run `uv lock` — the `just update-version` recipe does that.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
LIBS = REPO_ROOT / "libs"

BUMP_KEYWORDS = ("patch", "minor", "major")


@dataclass(frozen=True)
class TemplateSpec:
    """A versioned template package in this monorepo.

    `package` is the directory under libs/; the python module is derived from it
    (hyphens -> underscores). `synced_charts` are the Helm charts under
    template/charts/ whose version tracks the template version (SemVer spelling);
    charts not listed keep an independent version.
    """

    package: str
    synced_charts: tuple[str, ...] = ()

    @property
    def module(self) -> str:
        return self.package.replace("-", "_")

    @property
    def package_dir(self) -> Path:
        return LIBS / self.package

    @property
    def template_dir(self) -> Path:
        return self.package_dir / self.module / "template"


# Register a new template here — that is the only change needed to version it.
TEMPLATES: dict[str, TemplateSpec] = {
    "eks-karpenter": TemplateSpec(
        package="inference-tf-aws-eks-karpenter",
        synced_charts=("inference-extension", "karpenter", "kro", "metrics", "storage"),
    ),
}


def read_pyproject_version(file_path: Path) -> str:
    with open(file_path, "rb") as f:
        return str(tomllib.load(f)["project"]["version"])


def compute_bumped_version(current: str, bump: str) -> str:
    """Bump the X.Y.Z core of `current` (dropping any pre-release suffix)."""
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", current)
    if not match:
        print(f"Error: cannot parse a X.Y.Z core from current version '{current}'")
        sys.exit(1)
    major, minor, patch = (int(part) for part in match.groups())

    if bump == "major":
        major, minor, patch = major + 1, 0, 0
    elif bump == "minor":
        minor, patch = minor + 1, 0
    else:  # patch
        patch += 1
    return f"{major}.{minor}.{patch}"


def pep440_to_semver(version: str) -> str:
    """Convert a PEP 440 pre-release to this repo's SemVer spelling.

    Unlike jupyter-deploy's dotted form (`0.1.0rc1` -> `0.1.0-rc.1`), the charts here
    spell it WITHOUT a dot (`0.1.0rc1` -> `0.1.0-rc1`).
    """
    match = re.match(r"^(\d+\.\d+\.\d+)(rc|a|b)(\d+)$", version)
    if match:
        base, pre_type, pre_num = match.groups()
        return f"{base}-{pre_type}{pre_num}"
    return version


def _sub(file_path: Path, pattern: str, replacement: str, *, label: str) -> None:
    content = file_path.read_text()
    updated = re.sub(pattern, replacement, content, count=1, flags=re.MULTILINE)
    if content == updated:
        print(f"! Warning: no {label} updated in {file_path.relative_to(REPO_ROOT)}")
    else:
        file_path.write_text(updated)
        print(f"✓ Updated {label} in {file_path.relative_to(REPO_ROOT)}")


def upgrade(spec: TemplateSpec, new_version: str) -> None:
    semver = pep440_to_semver(new_version)

    # PEP 440, verbatim — the four files test_version.py asserts are byte-equal.
    _sub(
        spec.package_dir / "pyproject.toml",
        r'^(version\s*=\s*)["\'][^"\']+["\']',
        rf'\g<1>"{new_version}"',
        label="version",
    )
    _sub(
        spec.package_dir / spec.module / "__init__.py",
        r'^(__version__\s*=\s*)["\'][^"\']+["\']',
        rf'\g<1>"{new_version}"',
        label="__version__",
    )
    _sub(
        spec.template_dir / "manifest.yaml",
        r"^(\s+version:\s*).+$",
        rf"\g<1>{new_version}",
        label="template.version",
    )
    _sub(
        spec.template_dir / "engine" / "main.tf",
        r'(template_version\s*=\s*)["\'][^"\']+["\']',
        rf'\g<1>"{new_version}"',
        label="template_version",
    )

    # SemVer (undotted pre-release) — the synced Helm charts.
    for chart in spec.synced_charts:
        _sub(
            spec.template_dir / "charts" / chart / "Chart.yaml",
            r"^(version:\s*).+$",
            rf"\g<1>{semver}",
            label=f"{chart} chart version",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Bump one template's version across all files that pin it.")
    parser.add_argument("template", choices=sorted(TEMPLATES), help="which template to bump")
    parser.add_argument(
        "bump_or_version",
        nargs="?",
        default="patch",
        help="patch | minor | major (default: patch), or an explicit version (e.g. 0.1.0rc2)",
    )
    args = parser.parse_args()

    spec = TEMPLATES[args.template]
    current = read_pyproject_version(spec.package_dir / "pyproject.toml")
    if args.bump_or_version in BUMP_KEYWORDS:
        new_version = compute_bumped_version(current, args.bump_or_version)
    else:
        new_version = args.bump_or_version

    print(f"{args.template}: {current} -> {new_version} (charts: {pep440_to_semver(new_version)})\n")
    upgrade(spec, new_version)
    print("\nRun `uv lock` to update the lockfile (the just recipe does this).")


if __name__ == "__main__":
    main()
