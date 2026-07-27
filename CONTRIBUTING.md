# Contributor Guide

This project is a monorepo of [jupyter-deploy](https://github.com/jupyter-infra/jupyter-deploy)
templates that provision AWS EKS clusters for inference workloads. It uses
[uv](https://docs.astral.sh/uv/) to manage a workspace of template packages, and
[just](https://github.com/casey/just) as the command runner.

The `jupyter-deploy` CLI (`jd`) is **not** source-controlled here — it is consumed as a
published PyPI dependency (`jupyter-deploy[aws,k8s]`).

## Prerequisites

- install [uv](https://github.com/astral-sh/uv)
- install [just](https://github.com/casey/just)
- for template work: [terraform](https://developer.hashicorp.com/terraform/tutorials/aws-get-started/install-cli),
  the [aws-cli](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html),
  [kubectl](https://kubernetes.io/docs/tasks/tools/), and [helm](https://helm.sh/docs/intro/install/).

## Project setup

Fork and clone the repository, then create the virtual environment and install all workspace
packages:

```bash
just sync   # uv sync --all-packages
source .venv/bin/activate
```

## Run tools

This project uses [ruff](https://docs.astral.sh/ruff/) (lint/format/imports),
[mypy](https://mypy-lang.org/) (type checking), [yamllint](https://yamllint.readthedocs.io/),
`terraform fmt`, and [pytest](https://docs.pytest.org/).

```bash
just lint        # ruff format, ruff check --fix, mypy, terraform fmt, yamllint
just unit-test   # uv run pytest
```

CI runs the same checks on a `3.12`/`3.13` python matrix (see
[`.github/AGENT.md`](.github/AGENT.md)).

## Code review

Pull requests are reviewed automatically by [roborev](https://roborev.io), which posts a
review comment on each revision. Run the same review locally before pushing:

Install roborev from [roborev.io](https://roborev.io), then:

```bash
just review
```

This reviews your current branch against `main` using whatever AI coding agent you have
installed. It shares the policy file (`.roborev.toml`) with CI. CI runs the full matrix defined
there (`default` and `security` review types); a local run does a single review, so push to get
the complete CI review.

To review every commit automatically instead of on demand (opt-in):

```bash
just review-setup   # one-time: installs the roborev post-commit hook
```

Local review is optional; CI reviews every PR regardless.

## Bumping the template version

Each template is independently versioned, and its version is pinned in several files that MUST
stay in sync (enforced by that template's `tests/unit/test_version.py`). Bump one template with
one command — pass the template key:

```bash
just update-version eks-karpenter patch      # or minor | major
just update-version eks-karpenter 0.1.0rc2   # or an explicit version
```

This updates the target's `pyproject.toml`, `__init__.py`, `manifest.yaml`, `main.tf`, and its
synced Helm charts, then relocks (`uv lock`). Register a new template by adding a `TemplateSpec`
to `TEMPLATES` in `scripts/upgrade_template_version.py`.

## Work on templates

Templates are infrastructure-as-code blueprints. See [`AGENT.md`](AGENT.md) for the full
workflow: scaffolding a local `sandbox*` project with `jd init`, configuring it, deploying,
running configuration/E2E tests, and tearing it down.

Key rules (see `AGENT.md` for the complete set):

- all terraform variables live in `engine/variables.tf` (no defaults there — defaults go in
  `engine/presets/defaults-all.tfvars`); no `variable` blocks in other files
- `main.tf` declares `template_name` and `template_version` locals kept in sync with the
  package version