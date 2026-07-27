# .github — CI Workflows

## Workflows

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `ci.yml` | push/PR to `main` | Lint + unit tests (calls `lint.yml` + `test.yml`) |
| `lint.yml` | `workflow_call` | Reusable: ruff check, ruff format --check, mypy, yamllint, terraform fmt |
| `test.yml` | `workflow_call` | Reusable: `uv run pytest` (unit tests) |
| `roborev-review.yml` | `pull_request_target` / `workflow_dispatch` | Automated PR review via roborev + claude-code on Bedrock |
| `review-build-image.yml` | `workflow_dispatch` / push to `.github/review/**` | Build + push the roborev review image to the CI-account ECR |

This is a **single-package** uv workspace, so `ci.yml` runs lint/test against the repo root
(`working-directory: '.'`) — there is no affected-dirs discovery/matrix like jupyter-deploy has.

Both `lint.yml` and `test.yml` run a `['3.12', '3.13']` python matrix and install **unpinned**
deps (`rm -f uv.lock && uv sync --all-extras`) so CI catches upstream breakage early. CI pins
`terraform_version: "1.5.7"` for reproducible `terraform fmt` checks.

## roborev review

Copied from jupyter-deploy; fully driven by repo variables and **inert until they are set**,
so it can live on `main` before the CI account exists. Policy is in [`.roborev.toml`](../.roborev.toml)
(shared with local `just review`). The review image is built from [`review/Dockerfile`](review/Dockerfile)
(roborev + claude-code + gh + jq).

**Publishing the review image** — `review-build-image.yml` assumes the publish role, builds
`review/Dockerfile` (via the `just ci-review-*` recipes), and pushes to the CI-account ECR repo.
It is inert until its two vars are set; trigger it once via `workflow_dispatch` to publish the
first image, after which it rebuilds automatically on pushes to `main` touching `review/**`.
`roborev-review.yml` then *consumes* that published image. So the wiring order is: deploy
CI-infra → set the two publish vars below → run `review-build-image.yml` → set the four consume
vars → roborev goes live.

**Repo variables — publish** (set after `jupyter-infra-tf-aws-iam-ci` is deployed with
`create_review_resources = true`):

| Variable | Source (tf output) |
|---|---|
| `REVIEW_PUBLISH_ROLE_ARN` | `review_publish_iam_role_arn` |
| `REVIEW_ECR_REPOSITORY` | `review_image_repository_url` |
| `REVIEW_AWS_REGION` | optional, defaults `us-east-1` |

**Repo variables — consume** (set after the review image is published):

| Variable | Source (tf output) |
|---|---|
| `REVIEW_RUN_ROLE_ARN` | `review_run_iam_role_arn` |
| `REVIEW_IMAGE` | `review_image_repository_url` + `:latest` |
| `REVIEW_BEDROCK_MODEL` | `us.anthropic.claude-opus-4-8` (same as jupyter-deploy) |
| `REVIEW_AWS_REGION` | optional, defaults `us-east-1` |

**SECURITY:** `pull_request_target` runs with repo credentials, so the job never executes PR
code — it checks out the trusted base branch, fetches the PR head as git objects only, and
hands roborev a ref range. The container holds only the mounted Bedrock creds, never the
`GITHUB_TOKEN` (the runner posts the comment). The `review` GitHub Actions environment **MUST**
have protection rules (required reviewers and/or restricted branches): the run role's OIDC
trust only proves a job *declared* the `review` environment, not that the workflow was trusted.

Smoke-test the OIDC→Bedrock chain without running a real review via the `workflow_dispatch`
`smoke: true` input.

## CI infrastructure account

The e2e role, ECR repos, test-results bucket, and roborev review resources come from deploying
the `jupyter-infra-tf-aws-iam-ci` template (from the jupyter-deploy repo) into a dedicated CI
account. Use the template as-is with throwaway OAuth/bot values (this repo has no GitHub apps),
reuse ECR slot 1 for the e2e image, and set `create_review_resources = true`,
`publish_repo = inference-clusters`, `review_repos = [inference-clusters]`. The `jd` remote
store S3 bucket is created and managed by `jd` itself, not by this template.

## Testing Workflow Changes

`workflow_dispatch`/`pull_request_target` behavior is hard to iterate on from a branch. To test
a reusable (`workflow_call`) workflow, create a temporary push-triggered wrapper:

```yaml
# .github/workflows/test-<name>.yml  — DO NOT merge to main
name: Test workflow (temporary)
on:
  push:
    branches: [your-branch]
permissions:
  id-token: write    # required — reusable workflows inherit caller permissions
  contents: read
jobs:
  test:
    uses: ./.github/workflows/lint.yml
    with:
      working-directory: '.'
```

- The caller **must** declare any `permissions` the reusable workflow needs (e.g.
  `id-token: write` for OIDC) — reusable workflows inherit the caller's permissions.
- Remove or gitignore the test workflow before merging.
