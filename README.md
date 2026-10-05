# Inference Clusters

Monorepo of [jupyter-deploy](https://github.com/jupyter-infra/jupyter-deploy) templates that
provision **EKS clusters for inference workloads**.

These templates reuse the `jupyter-deploy` (`jd`) CLI to scaffold, configure, and deploy
the cluster infrastructure with a few simple commands. Using `jd` to manage inference clusters
is a deliberate shortcut for a proof-of-concept: it gives us templated Terraform, presets,
config/up/down lifecycle, and a manifest-driven command surface for free.

## Repository layout

This repository is a [uv](https://github.com/astral-sh/uv) workspace. The `jupyter-deploy` CLI core
is consumed as a published PyPI dependency; each template package under `libs/` is a workspace member
that registers itself with the CLI via a `jupyter_deploy.terraform_templates` entry point.

## Packages

- [inference-tf-aws-eks-karpenter](./libs/inference-tf-aws-eks-karpenter/README.md):
  A Terraform template that provisions a base AWS EKS cluster with [Karpenter](https://karpenter.sh)
  for node autoscaling over **self-managed nodes**, intended as the foundation for inference workloads.

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- [just](https://github.com/casey/just#installation) (e.g. `brew install just`, `cargo install just`,
  or see the install guide for your platform)

## Getting started

```bash
# create the virtual environment and install the workspace
uv sync

# the jd CLI is available with the inference templates registered
uv run jd --help
```

## Development

```bash
# lint your changes
just lint

# run the unit tests
just unit-test
```

See [AGENT.md](./AGENT.md) for repository conventions.

## External Dependencies

This package depends on and may incorporate or retrieve a number of third-party
software packages (such as open source packages) at install-time or build-time
or run-time ("External Dependencies"). The External Dependencies are subject to
license terms that you must accept in order to use this package. If you do not
accept all of the applicable license terms, you should not use this package. We
recommend that you consult your company’s open source approval policy before
proceeding.

Provided below is a list of External Dependencies and the applicable license
identification as indicated by the documentation associated with the External
Dependencies as of Amazon's most recent review.

THIS INFORMATION IS PROVIDED FOR CONVENIENCE ONLY. AMAZON DOES NOT PROMISE THAT
THE LIST OR THE APPLICABLE TERMS AND CONDITIONS ARE COMPLETE, ACCURATE, OR
UP-TO-DATE, AND AMAZON WILL HAVE NO LIABILITY FOR ANY INACCURACIES. YOU SHOULD
CONSULT THE DOWNLOAD SITES FOR THE EXTERNAL DEPENDENCIES FOR THE MOST COMPLETE
AND UP-TO-DATE LICENSING INFORMATION.

YOUR USE OF THE EXTERNAL DEPENDENCIES IS AT YOUR SOLE RISK. IN NO EVENT WILL
AMAZON BE LIABLE FOR ANY DAMAGES, INCLUDING WITHOUT LIMITATION ANY DIRECT,
INDIRECT, CONSEQUENTIAL, SPECIAL, INCIDENTAL, OR PUNITIVE DAMAGES (INCLUDING
FOR ANY LOSS OF GOODWILL, BUSINESS INTERRUPTION, LOST PROFITS OR DATA, OR
COMPUTER FAILURE OR MALFUNCTION) ARISING FROM OR RELATING TO THE EXTERNAL
DEPENDENCIES, HOWEVER CAUSED AND REGARDLESS OF THE THEORY OF LIABILITY, EVEN
IF AMAZON HAS BEEN ADVISED OF THE POSSIBILITY OF SUCH DAMAGES. THESE LIMITATIONS
AND DISCLAIMERS APPLY EXCEPT TO THE EXTENT PROHIBITED BY APPLICABLE LAW.

- Grafana (AGPL-3.0) — https://github.com/grafana/grafana — only deployed when
  `enable_grafana = true` (off by default).

## License

This project is licensed under the [MIT License](LICENSE).
