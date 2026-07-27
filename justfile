# List all available commands
default:
    @just --list

# Sync the uv workspace (create venv, install deps + workspace members)
sync:
    uv sync --all-packages

# Run all linting and formatting tools
lint:
    uv run ruff format
    uv run ruff check --preview --fix
    uv run mypy
    terraform fmt -recursive -write=true
    uv run yamllint .

# Run unit tests
unit-test:
    uv run pytest

# Bump one template's version across pyproject/__init__/manifest/main.tf + synced charts, then relock
# Templates are independently versioned; pass the template key (e.g. eks-karpenter).
# Usage: just update-version <template> [patch|minor|major|<explicit-version>]
update-version template bump="patch":
    uv run python scripts/upgrade_template_version.py {{template}} {{bump}}
    uv lock

# Review the current branch against main with roborev (shares .roborev.toml with CI)
review:
    #!/usr/bin/env bash
    set -euo pipefail
    command -v roborev >/dev/null 2>&1 || { echo "roborev not found. Install it from https://roborev.io, then optionally run 'just review-setup'."; exit 1; }
    roborev review --branch --local --wait

# Opt-in: install the roborev post-commit hook for continuous local review
review-setup:
    #!/usr/bin/env bash
    set -euo pipefail
    command -v roborev >/dev/null 2>&1 || { echo "roborev not found. Install it from https://roborev.io first."; exit 1; }
    roborev init --agent claude-code

# ==============================================================================
# roborev review image (published to the CI-account ECR by review-build-image.yml)
# ==============================================================================
# The review image (roborev + claude-code agent + gh) is published occasionally —
# when its pinned tool versions in .github/review/Dockerfile change — not per PR.
# roborev-review.yml pulls it from ECR at review time.

# Build the roborev review image (self-contained: roborev + claude-code agent + gh)
# Usage: just ci-review-build                     # local: no cache
# Usage: just ci-review-build <ecr-url>:latest    # CI: use ECR :latest as layer cache
ci-review-build cache_from="":
    #!/usr/bin/env bash
    set -euo pipefail

    CACHE_ARG=""
    if [ -n "{{cache_from}}" ]; then
        CACHE_ARG="--cache-from={{cache_from}}"
        echo "Using cache from: {{cache_from}}"
    fi

    {{container-tool}} build \
        -f .github/review/Dockerfile \
        $CACHE_ARG \
        -t inference-review:latest \
        .

    echo "✓ Review image built: inference-review:latest"

# Push the roborev review image to ECR
# Usage: just ci-review-push <ecr-repo-url> [extra-tag]
# Pushes as :latest, and also as :<extra-tag> if provided (e.g. git sha)
ci-review-push ecr_url extra_tag="":
    #!/usr/bin/env bash
    set -euo pipefail

    ECR_REGISTRY=$(echo "{{ecr_url}}" | cut -d'/' -f1)
    REGION=$(echo "{{ecr_url}}" | cut -d'.' -f4)

    echo "Logging in to ECR..."
    aws ecr get-login-password --region "$REGION" \
        | {{container-tool}} login --username AWS --password-stdin "$ECR_REGISTRY"

    echo "Pushing to {{ecr_url}}..."
    {{container-tool}} tag inference-review:latest "{{ecr_url}}:latest"
    {{container-tool}} push "{{ecr_url}}:latest"

    if [ -n "{{extra_tag}}" ]; then
        {{container-tool}} tag inference-review:latest "{{ecr_url}}:{{extra_tag}}"
        {{container-tool}} push "{{ecr_url}}:{{extra_tag}}"
        echo "✓ Pushed {{ecr_url}}:latest and {{ecr_url}}:{{extra_tag}}"
    else
        echo "✓ Pushed {{ecr_url}}:latest"
    fi

# Pull the roborev review image from ECR and tag as inference-review:latest
# Usage: just ci-review-pull <ecr-repo-url> [tag]
ci-review-pull ecr_url tag="latest":
    #!/usr/bin/env bash
    set -euo pipefail

    ECR_REGISTRY=$(echo "{{ecr_url}}" | cut -d'/' -f1)
    REGION=$(echo "{{ecr_url}}" | cut -d'.' -f4)

    echo "Logging in to ECR..."
    aws ecr get-login-password --region "$REGION" \
        | {{container-tool}} login --username AWS --password-stdin "$ECR_REGISTRY"

    echo "Pulling {{ecr_url}}:{{tag}}..."
    {{container-tool}} pull "{{ecr_url}}:{{tag}}"
    {{container-tool}} tag "{{ecr_url}}:{{tag}}" inference-review:latest
    echo "✓ Pulled and tagged as inference-review:latest"

# ==============================================================================
# Vendored CRDs — Gateway API Inference Extension (InferencePool)
# ==============================================================================
# The inference-extension CRDs are vendored (committed) rather than pulled at
# deploy time — upstream publishes no Helm chart, only a raw manifest. This recipe
# re-vendors them from a pinned upstream release so bumping the version is one
# command, not a hand-edit of ~800 lines. Change the version below and re-run.
inference-extension-crd-version := "v1.0.1"

# Re-vendor the InferencePool CRDs from the pinned upstream release.
# Usage: just update-inference-extension-crds [version=vX.Y.Z]
update-inference-extension-crds version=inference-extension-crd-version:
    #!/usr/bin/env bash
    set -euo pipefail
    dest="libs/inference-tf-aws-eks-karpenter/inference_tf_aws_eks_karpenter/template/charts/inference-extension/templates/crds.yaml"
    url="https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/download/{{version}}/manifests.yaml"
    echo "re-vendoring inference-extension CRDs from {{version}} ..."
    body="$(mktemp)"
    curl -fsSL "$url" -o "$body"
    count="$(grep -c '^kind: CustomResourceDefinition' "$body" || true)"
    if [ "$count" -eq 0 ]; then echo "error: no CRDs found at $url" >&2; exit 1; fi
    {
      echo "# Vendored verbatim from Gateway API Inference Extension {{version}}"
      echo "# $url"
      echo "# ${count} CustomResourceDefinitions: inferencepools (GA inference.networking.k8s.io),"
      echo "# inferencepools + inferenceobjectives (alpha inference.networking.x-k8s.io the EPP watches)."
      echo "# DO NOT hand-edit — run 'just update-inference-extension-crds' to change versions. Pinned to"
      echo "# match the EPP image used by the workload chart (inference-charts)."
      cat "$body"
    } > "$dest"
    rm -f "$body"
    echo "wrote ${count} CRDs to $dest"

# ==============================================================================
# E2E test harness (via pytest-jupyter-deploy)
# ==============================================================================
# The E2E container image is template-independent and vended by the
# pytest-jupyter-deploy package. Config-only tests need no AWS; full-deployment
# tests (full-deploy=true) provision a real cluster and require AWS credentials.

# Detect container tool (finch or docker)
container-tool := `command -v finch >/dev/null 2>&1 && echo "finch" || echo "docker"`

# Host user UID/GID for running containers with correct permissions
export HOST_UID := `id -u`
export HOST_GID := `id -g`

# E2E image configuration (base compose vended by pytest-jupyter-deploy).
# The base compose file hardcodes container_name/image as "jupyter-deploy-e2e"; we
# merge a committed override (docker-compose.e2e-name.yml) so this repo's container
# and image get distinct names and never collide with a jupyter-deploy E2E run on the
# same host. E2E_IMAGE steers the image ${E2E_IMAGE:-...} interpolation in the base file.
#
# The base compose dir is discovered from pytest-jupyter-deploy at runtime, INSIDE each
# E2E recipe body (E2E_IMAGE_DIR / E2E_COMPOSE_FILES) rather than a top-level `:=` backtick.
# `just` eagerly evaluates every top-level backtick assignment before running ANY recipe,
# so a top-level discovery here would make `just sync`/`lint`/`unit-test` fail on a fresh
# clone (pytest-jupyter-deploy isn't installed yet) — a bootstrap deadlock. E2E recipes
# run post-sync, so resolving lazily keeps them working while freeing the core recipes.
e2e-container-name := "inference-e2e"
e2e-image-tag := "latest"
export E2E_IMAGE := "inference-e2e:latest"

# helm is NOT in the vended pytest-jupyter-deploy image (which ships terraform/aws/kubectl/yq);
# the serving e2e tests (test_vllm_serving etc.) shell out to `helm install`, so e2e-up installs
# a pinned binary into the running container. Bump here to move helm versions.
e2e-helm-version := "v3.16.3"

# Template under test (matches libs/<template>/tests/e2e)
default-template := "inference-tf-aws-eks-karpenter"

# Start E2E container in background (always builds to ensure correct UID/GID)
# Usage: just e2e-up [no-cache=true]
e2e-up no_cache="false":
    #!/usr/bin/env bash
    set -euo pipefail

    E2E_IMAGE_DIR="$(uv run python -c 'from pytest_jupyter_deploy.image import IMAGE_PATH; print(IMAGE_PATH)')"
    E2E_COMPOSE_FILES="-f $E2E_IMAGE_DIR/docker-compose.yml -f {{justfile_directory()}}/docker-compose.e2e-name.yml"

    echo "Building and starting E2E container (HOST_UID={{HOST_UID}}, HOST_GID={{HOST_GID}})..."
    mkdir -p {{justfile_directory()}}/test-results
    mkdir -p {{justfile_directory()}}/.auth

    # Ensure a .env exists (compose reads it)
    if [ ! -f {{justfile_directory()}}/.env ]; then
        cp {{justfile_directory()}}/env.example {{justfile_directory()}}/.env
    fi

    sed -i 's/^HOST_UID=.*/HOST_UID={{HOST_UID}}/' {{justfile_directory()}}/.env
    sed -i 's/^HOST_GID=.*/HOST_GID={{HOST_GID}}/' {{justfile_directory()}}/.env
    if grep -q '^E2E_DOCKERFILE=' {{justfile_directory()}}/.env; then
        sed -i "s|^E2E_DOCKERFILE=.*|E2E_DOCKERFILE=$E2E_IMAGE_DIR/Dockerfile|" {{justfile_directory()}}/.env
    else
        echo "E2E_DOCKERFILE=$E2E_IMAGE_DIR/Dockerfile" >> {{justfile_directory()}}/.env
    fi
    # Resolve AWS_REGION (SDK treats "" as a valid-but-broken region)
    _AWS_REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || echo "")}"
    if [ -n "$_AWS_REGION" ]; then
        if grep -q '^AWS_REGION=' {{justfile_directory()}}/.env; then
            sed -i "s|^AWS_REGION=.*|AWS_REGION=$_AWS_REGION|" {{justfile_directory()}}/.env
        else
            echo "AWS_REGION=$_AWS_REGION" >> {{justfile_directory()}}/.env
        fi
    fi

    if [ "{{no_cache}}" = "true" ]; then
        {{container-tool}} compose --project-directory {{justfile_directory()}} $E2E_COMPOSE_FILES build --no-cache
    else
        {{container-tool}} compose --project-directory {{justfile_directory()}} $E2E_COMPOSE_FILES build
    fi

    mkdir -p ~/.kube  # must exist before compose up; Docker creates missing bind-mount sources as root
    {{container-tool}} compose --project-directory {{justfile_directory()}} $E2E_COMPOSE_FILES up -d e2e
    just e2e-ensure-helm
    echo "E2E container started. Syncing latest code..."
    just e2e-sync
    echo "✓ E2E container ready"

# Install helm into the running E2E container if absent. helm is NOT in the vended
# pytest-jupyter-deploy image (which ships terraform/aws/kubectl/yq), but the serving e2e
# tests (test_vllm_serving etc.) shell out to `helm install`. No-op once the image bakes
# helm in (ANY version) — at which point this recipe and its callers can be dropped. Must
# run after EVERY container (re)creation: `test-e2e` recreates from the image, discarding
# anything installed into a prior running container.
e2e-ensure-helm:
    #!/usr/bin/env bash
    set -euo pipefail
    if {{container-tool}} exec {{e2e-container-name}} bash -c 'command -v helm >/dev/null 2>&1'; then
        echo "helm already present in the E2E container (no-op)"
        exit 0
    fi
    echo "Installing helm {{e2e-helm-version}} into the E2E container..."
    {{container-tool}} exec --user root {{e2e-container-name}} bash -c '\
        set -e; \
        curl -fsSL "https://get.helm.sh/helm-{{e2e-helm-version}}-linux-amd64.tar.gz" \
            | tar -xz -C /tmp linux-amd64/helm; \
        install -m 0755 /tmp/linux-amd64/helm /usr/local/bin/helm; \
        rm -rf /tmp/linux-amd64; \
        helm version --short'

# Stop E2E container
e2e-down:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Stopping E2E container..."
    E2E_IMAGE_DIR="$(uv run python -c 'from pytest_jupyter_deploy.image import IMAGE_PATH; print(IMAGE_PATH)')"
    E2E_COMPOSE_FILES="-f $E2E_IMAGE_DIR/docker-compose.yml -f {{justfile_directory()}}/docker-compose.e2e-name.yml"
    {{container-tool}} compose --project-directory {{justfile_directory()}} $E2E_COMPOSE_FILES down

# Sync workspace files into the running E2E container
e2e-sync:
    #!/usr/bin/env bash
    set -euo pipefail

    E2E_IMAGE_DIR="$(uv run python -c 'from pytest_jupyter_deploy.image import IMAGE_PATH; print(IMAGE_PATH)')"
    E2E_COMPOSE_FILES="-f $E2E_IMAGE_DIR/docker-compose.yml -f {{justfile_directory()}}/docker-compose.e2e-name.yml"

    if ! ({{container-tool}} compose --project-directory {{justfile_directory()}} $E2E_COMPOSE_FILES ps e2e) | grep -qE "(Up|running)"; then
        echo "Error: E2E container is not running. Start it with: just e2e-up"
        exit 1
    fi

    echo "Syncing project files to E2E container..."
    {{container-tool}} exec {{e2e-container-name}} bash -c "rm -rf /workspace/.venv"

    cd {{justfile_directory()}} && \
    tar --exclude='.venv' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.pytest_cache' \
        --exclude='test-results' \
        --exclude='.git' \
        --exclude='.ruff_cache' \
        --exclude='.mypy_cache' \
        --exclude='sandbox*' \
        --exclude='.auth' \
        -cf - . | \
    {{container-tool}} exec -i {{e2e-container-name}} tar -xf - -C /workspace

    echo "Running uv sync..."
    {{container-tool}} compose --project-directory {{justfile_directory()}} $E2E_COMPOSE_FILES exec e2e bash -c "cd /workspace && uv sync --all-packages"
    echo "✓ E2E container synced"

# Run E2E tests inside the container.
# Usage: just test-e2e [project-dir] [test-filter] [options] [template]
# Options: comma-separated key=value pairs, recognized keys:
#   full-deploy=true   include full_deployment tests (deploys real infra)
#   mutate=true        include mutating tests
#   destroy=true       destroy after a from-scratch deploy
#   log-level=debug    pytest --log-cli-level
#   skip-sync=true     use the pre-built image as-is (don't re-sync the workspace)
#
# Example: just test-e2e sandbox-e2e test_configuration              # config-only, no AWS
# Example: just test-e2e sandbox-e2e "" full-deploy=true,destroy=true # full deploy + teardown
test-e2e project_dir="sandbox-e2e" test_filter="" options="" template=default-template:
    #!/usr/bin/env bash
    set -euo pipefail

    OVERRIDE_FILE=""
    cleanup() { [ -n "$OVERRIDE_FILE" ] && [ -f "$OVERRIDE_FILE" ] && rm -f "$OVERRIDE_FILE"; }
    trap cleanup EXIT

    E2E_IMAGE_DIR="$(uv run python -c 'from pytest_jupyter_deploy.image import IMAGE_PATH; print(IMAGE_PATH)')"
    E2E_COMPOSE_FILES="-f $E2E_IMAGE_DIR/docker-compose.yml -f {{justfile_directory()}}/docker-compose.e2e-name.yml"

    # Determine deploy-from-scratch vs existing-project mode.
    IS_DEPLOYMENT_FROM_SCRATCH="false"
    case "{{project_dir}}" in
        sandbox-*)
            if [ -d "{{project_dir}}" ] && [ -n "$(ls -A {{project_dir}} 2>/dev/null)" ]; then
                echo "Mode: Test existing project ({{project_dir}})"
            else
                IS_DEPLOYMENT_FROM_SCRATCH="true"
                mkdir -p "{{project_dir}}"
                echo "Mode: Deploy from scratch ({{project_dir}})"
            fi
            ;;
        *)
            if [ ! -d "{{project_dir}}" ]; then
                echo "Error: Project directory '{{project_dir}}' does not exist"
                exit 1
            fi
            echo "Mode: Test existing project ({{project_dir}})"
            ;;
    esac

    mkdir -p "{{justfile_directory()}}/test-results"
    mkdir -p "{{justfile_directory()}}/.auth"
    rm -rf "{{justfile_directory()}}/test-results"/*

    SKIP_SYNC="false"
    IMAGE="{{e2e-container-name}}:{{e2e-image-tag}}"
    if echo "{{options}}" | grep -q "skip-sync=true"; then SKIP_SYNC="true"; fi
    if echo "{{options}}" | grep -qE "image="; then
        IMAGE=$(echo "{{options}}" | grep -oE "image=[^,]+" | cut -d'=' -f2)
    fi

    # Generate the compose override mounting the project dir + test-results.
    OVERRIDE_FILE="{{justfile_directory()}}/docker-compose.e2e-override.yml"
    {
        echo "services:"
        echo "  e2e:"
        if [ "$SKIP_SYNC" = "true" ]; then echo "    image: $IMAGE"; fi
        echo "    volumes:"
        echo "      - ./{{project_dir}}:/workspace/{{project_dir}}"
        echo "      - ./test-results:/workspace/test-results"
    } > "$OVERRIDE_FILE"

    echo "Restarting E2E container with project mount..."
    mkdir -p ~/.kube
    {{container-tool}} compose --project-directory {{justfile_directory()}} $E2E_COMPOSE_FILES down
    {{container-tool}} compose --project-directory {{justfile_directory()}} $E2E_COMPOSE_FILES -f "$OVERRIDE_FILE" up -d --no-build

    # The recreate above restored the container from the image, dropping any helm installed
    # into the previous running container — reinstall it (no-op once the image bakes it in).
    just e2e-ensure-helm

    if [ "$SKIP_SYNC" != "true" ]; then
        echo "Re-syncing project files after mount..."
        just e2e-sync
    fi

    # Resolve the E2E tests directory for the template.
    E2E_TESTS_DIR="libs/{{template}}/tests/e2e"
    if [ ! -d "$E2E_TESTS_DIR" ]; then
        echo "Error: Could not find E2E tests directory: $E2E_TESTS_DIR"
        exit 1
    fi

    if [ "$IS_DEPLOYMENT_FROM_SCRATCH" = "true" ]; then
        PYTEST_ARGS="$E2E_TESTS_DIR -m e2e --e2e-tests-dir=$E2E_TESTS_DIR"
    else
        PYTEST_ARGS="$E2E_TESTS_DIR -m e2e --e2e-tests-dir=$E2E_TESTS_DIR --e2e-existing-project={{project_dir}}"
    fi

    if [ -n "{{test_filter}}" ]; then PYTEST_ARGS="$PYTEST_ARGS -k {{test_filter}}"; fi

    LOG_LEVEL="INFO"
    if echo "{{options}}" | grep -q "full-deploy=true"; then
        PYTEST_ARGS="$PYTEST_ARGS --with-full-deployment"
        echo "  - full deployment tests: enabled"
    fi
    if echo "{{options}}" | grep -q "mutate=true"; then
        PYTEST_ARGS="$PYTEST_ARGS --with-mutating-cases"
        echo "  - mutating tests: enabled"
    fi
    if echo "{{options}}" | grep -q "destroy=true"; then
        PYTEST_ARGS="$PYTEST_ARGS --destroy-after"
        echo "  - destroy after tests: enabled"
    fi
    if echo "{{options}}" | grep -qE "log-level=(info|debug|warning|error)"; then
        LOG_LEVEL=$(echo "{{options}}" | grep -oE "log-level=(info|debug|warning|error)" | cut -d'=' -f2 | tr '[:lower:]' '[:upper:]')
    fi

    PYTEST_ARGS="$PYTEST_ARGS --verbose --log-cli-level=$LOG_LEVEL"

    if [ "$SKIP_SYNC" = "true" ]; then
        PYTEST_CMD=". .venv/bin/activate && pytest"
    else
        PYTEST_CMD="uv run pytest"
    fi

    echo "Running E2E tests (template={{template}}, project={{project_dir}})..."
    echo "================================================"
    {{container-tool}} compose --project-directory {{justfile_directory()}} $E2E_COMPOSE_FILES exec -e PYTHONUNBUFFERED=1 e2e bash -c "cd /workspace && $PYTEST_CMD $PYTEST_ARGS"

# Convenience wrapper for the EKS Karpenter template.
# Example: just test-e2e-eks-karpenter sandbox-e2e test_configuration            # config only, no AWS
# Example: just test-e2e-eks-karpenter sandbox-e2e "" full-deploy=true,destroy=true
test-e2e-eks-karpenter project_dir="sandbox-e2e" test_filter="" options="":
    @just test-e2e {{project_dir}} "{{test_filter}}" "{{options}}" inference-tf-aws-eks-karpenter

# Full workflow: start container (builds if needed) then run tests
e2e-all project_dir="sandbox-e2e" test_filter="" options="" no_cache="false" template=default-template:
    @just e2e-up {{no_cache}}
    @just test-e2e {{project_dir}} "{{test_filter}}" "{{options}}" {{template}}

# Clean up E2E artifacts and image
clean-e2e:
    #!/usr/bin/env bash
    set -uo pipefail
    rm -rf test-results .pytest_cache docker-compose.e2e-override.yml
    # Best-effort: resolve the base compose dir; if deps aren't synced, skip compose down.
    E2E_IMAGE_DIR="$(uv run python -c 'from pytest_jupyter_deploy.image import IMAGE_PATH; print(IMAGE_PATH)' 2>/dev/null || true)"
    if [ -n "$E2E_IMAGE_DIR" ]; then
        E2E_COMPOSE_FILES="-f $E2E_IMAGE_DIR/docker-compose.yml -f {{justfile_directory()}}/docker-compose.e2e-name.yml"
        {{container-tool}} compose --project-directory {{justfile_directory()}} $E2E_COMPOSE_FILES down -v || true
    fi
    {{container-tool}} rmi {{e2e-container-name}}:{{e2e-image-tag}} || true
