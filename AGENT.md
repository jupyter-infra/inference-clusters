<!-- CLAUDE.md is a symlink to this file -->

# Project Context

This is a monorepo of [jupyter-deploy](https://github.com/jupyter-infra/jupyter-deploy)
templates that provision **AWS EKS clusters for inference workloads**.

The `jupyter-deploy` CLI (`jd`) is **not** source-controlled here — it is consumed as a published
PyPI dependency (`jupyter-deploy[aws,k8s]`). This repo only ships **template packages**:
data payloads (Terraform, manifests, presets) plus a thin Python shim that registers each
template with the CLI through a `jupyter_deploy.terraform_templates` entry point.

## Workspace layout

uv workspace. Each publishable template is a member under `libs/`:

```
libs/<pkg>/
├── pyproject.toml                       # entry-point registration + build (hatchling)
├── <module>/
│   ├── __init__.py                      # __version__
│   ├── template.py                      # TEMPLATE_PATH = .../template
│   └── template/                        # payload scaffolded into a user project by `jd init`
│       ├── manifest.yaml                # template metadata, provider commands, progress phases
│       ├── variables.yaml               # variable definitions + config presets
│       ├── AGENT.md.template            # template-specific AI instructions (rendered on init)
│       └── engine/                      # terraform: main.tf, variables.tf, outputs.tf, modules/, presets/
└── tests/{unit,e2e}/
```

## Template packages

### EKS Karpenter base template
Code: `./libs/inference-tf-aws-eks-karpenter`

- infrastructure-as-code engine: `terraform`
- cloud provider: `aws`
- node autoscaling: [Karpenter](https://karpenter.sh) over **self-managed nodes**

A base EKS cluster intended as the foundation for inference workloads. Karpenter
provisions and scales self-managed nodes on demand (rather than relying solely on
EKS managed node groups).

## Development workflow

This repo is a uv workspace. Common commands (see `justfile`):

- `just sync` — `uv sync` the workspace
- `just lint` — `ruff format`, `ruff check --fix`, `mypy`, `terraform fmt`, `yamllint`
- `just unit-test` — `uv run pytest`
- `just update-version <template> <patch|minor|major|version>` — bump one template's version

### General python coding rules
1. You MUST NOT silence linters without the user's permission.
2. you MUST NOT write docstrings that merely repeat a method name.

### Terraform conventions (for templates)
1. All variables MUST be defined in `engine/variables.tf` without default values.
  Default values MUST live in `engine/presets/defaults-all.tfvars`.
2. There MUST NOT be any `variable` blocks in files other than `variables.tf`.
3. In the `engine/variables.tf` of a template, each variable docstring MUST BE a single line,
  then optionally an empty line, further explanation, and a recommended: value. This matters
  for `jd` proper display of the message.
4. All `local-exec` provisioners MUST set `interpreter = ["/bin/bash", "-c"]` —
  Terraform defaults to `/bin/sh`.
5. `main.tf` MUST declare `template_name` and `template_version` locals; the version
  MUST stay in sync with `pyproject.toml`, `__init__.py`, and `manifest.yaml`
  (enforced by `tests/unit/test_*_version.py`).
6. `main.tf` MUST ensure two copies of a deployment may leave in the same account/region.
  Add a `random_id.postfix`, tag all cloud resources with it and ensure all resource names
  embed it so that there can be no name conflict between deployments.

### Writing unit tests
Unit tests live in `libs/<pkg>/tests/unit`.
1. Define a `unittest.TestCase` per class/function/major method under test.
2. Prefer `@patch()` / inline `with patch` over `pytest.fixtures`.
3. Always annotate patched args as `: Mock` for mypy.
4. Templates MUST have focused consistency tests for stable contracts and load-bearing
  dependency-graph invariants that `terraform validate` cannot catch. Do not mirror an
  implementation by asserting incidental file names, command text, or every internal
  step; cover observable behavior with E2E tests instead.

### Testing templates

Templates are infra-as-code blueprints for cloud resources.
Use the `jd` CLI and a template to create a new deployment in a specific directory.

When developing locally, you'll need a `<project-dir>`, which MUST be of the form `./sandbox*`,
in same dir as this `AGENT.md` file; these dirs are gitignored. Examples: `./sandbox`, `./sandbox-1`.
Ask the user which `<project-dir>` they want you to use. Suggest `sandbox` if it does not already exist locally.

To create a new project, `cd` into the empty `<project-dir>`, then:
- for `eks-karpenter` template, run: `jd init . -E terraform -P aws -I eks -T karpenter`

To restore an existing project from the `jd` remote store, `cd` into the empty `<project-dir>`, then:
- `jd projects list --store-type s3-only`: find the project to restore
- `jd init . --restore-project <PROJECT-NAME> --store-type s3-only`

Once initiated, you can:
- configure the `variables.yaml` files then run `jd config -v`
  - if that fails, read logs with `jd history show config`
- run `jd up -v` to apply the changes
- you can view logs with `jd history show up`

Once deployed, you can:
- get `kubeconfig` for that cluster with: `jd cluster login -p <project-dir>`

**IMPORTANT**: Deployment directories contain their own copy of template files.
When testing template changes in an existing deployment (e.g., `sandbox`),
you must manually copy the modified template files from `libs/<pkg>/<module>/template/`
to the deployment directory BEFORE running `jd config` & `jd up`.

**IMPORTANT**: A NEW variable added to the template won't apply to an existing deployment
until you also add it to that deployment's `<project-dir>/variables.yaml` `overrides:` block
(jd generates the tfvars from there at `jd init`; a fresh `jd init` picks it up automatically).

**IMPORTANT**: `jd up` automatically backs up the project to a remote store, in our case
an S3 bucket in the AWS account. After a full `jd up` run (even if it ends in the failure),
`jd` saves the project files and the terraform state in the store.
`jd` creates and manages this store bucket itself — it is NOT provisioned by the CI-infra template.

**IMPORTANT**: If a first `jd up` is interrupted before its local state is pushed to the store,
`terraform destroy` the partial state rather than bare re-running `jd up`,
which otherwise regenerates `random_id.postfix` and orphans a second full deployment.

To teardown a deployment, you should:
- confirm with the user that it's okay
- run `jd down -y -v`
- once fully succeeded:
    - remove store entry with `jd projects delete <PROJECT-ID> --store-type s3-only`
    - delete the local `<project-dir>`
- if you hit any issue:
    - see logs with `jd history show down`
    - possibly retry
    - `cd` to `<project-dir>/engine` and try the `terraform` command directly
- if all fails, you may need to lookup `aws` resources by tags (using `DeploymentId`)
  and clean them up manually. use the Resource Groups Tagging API to find them.
  **Note**: tags might not capture all resources, watch out for aws resources created
  by Kubernetes operators.

### Configuration Tests
The configuration test verifies a template project is correctly wired up.
In the case of the `eks-karpenter` template, this corresponds to the `terraform plan` operation succeeding.

To run the configuration test:
1. ask the user for the `<project-dir>` to use
2. if testing modified template files, ensure they've been copied to `<project-dir>`
3. run `just test-e2e-eks-karpenter <project-dir> test_project_is_configurable`

Note: `test_configuration` contains other test cases. Use `test_project_is_configurable`
for a quick validation that the template is configurable.

### E2E tests
E2E tests live in `libs/<pkg>/tests/e2e` and use the `pytest-jupyter-deploy`
plugin (the `e2e_deployment` fixture, `undeployed_project` helper, etc.). They run
inside a container whose image is **vended by `pytest-jupyter-deploy`** — there is no
Dockerfile in this repo.

The base compose file (vended by the plugin) hardcodes the container/image name to
`jupyter-deploy-e2e`. We merge a committed override (`docker-compose.e2e-name.yml`) on
every compose call so this repo's container/image are named `inference-e2e`.

Copy `env.example` to `.env` at the repo root before running (the recipes populate
`HOST_UID`/`HOST_GID`/`AWS_REGION` automatically).

Ask the user which `<project-dir>` to run the e2e tests against.

- `just e2e-up` — build + start the E2E container.
- `just test-e2e-eks-karpenter sandbox-e2e test_configuration` — run config-only tests
  (just `jd init`/`jd config`).
- `just test-e2e-eks-karpenter sandbox-e2e "" full-deploy=true` — provision a
  real cluster, run `full_deployment`-marked tests
- `just e2e-down` / `just clean-e2e` — stop / clean up.

Check the `justfile` for the container tool in use. It might either be `docker` or `finch`.
You can view the running containers with `<CONTAINER-TOOL> container list`.

### Making changes in the GitHub CI

Refer to [`.github/AGENT.md`](.github/AGENT.md).

### Automated code review

Open PRs are reviewed automatically in CI by [roborev](https://roborev.io)
(policy in [`.roborev.toml`](.roborev.toml)). Run the same review locally with `just review`.
