"""E2E tests for project scaffolding and configuration.

These tests do NOT require AWS credentials or a deployed cluster — they validate
that `jd init`/`jd config` produce a correctly wired project from the template.
They run inside the pytest-jupyter-deploy E2E container.
"""

import re

from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.undeployed_project import undeployed_project

# jd snippet placeholders are `{{ some-hyphenated-name }}` (e.g. {{ health-check-instructions }})
# and must all be substituted at init. This must NOT match the helm `chart.image` helper the
# template ships verbatim for consumers ({{- define ... }}, {{ .Values... }}) — those have
# dots, quotes, or `-}}` trim markers, none of which fit the snippet-token shape.
_JD_PLACEHOLDER_RE = re.compile(r"\{\{ [a-z][a-z0-9-]* \}\}")


def test_project_scaffolds_from_template(e2e_deployment: EndToEndDeployment) -> None:
    """`jd init` produces the expected project files from the template."""
    with undeployed_project(e2e_deployment.suite_config) as (project_path, _cli):
        for relative in ["manifest.yaml", "variables.yaml", "AGENT.md", ".gitignore"]:
            assert (project_path / relative).exists(), f"missing scaffolded file: {relative}"

        engine_dir = project_path / "engine"
        assert engine_dir.is_dir(), "engine/ directory should exist after init"
        for tf_file in ["main.tf", "variables.tf", "outputs.tf"]:
            assert (engine_dir / tf_file).exists(), f"missing engine file: {tf_file}"


def test_agent_md_rendered_after_init(e2e_deployment: EndToEndDeployment) -> None:
    """AGENT.md is rendered (snippets substituted) and the template removed."""
    with undeployed_project(e2e_deployment.suite_config) as (project_path, _cli):
        agent_path = project_path / "AGENT.md"
        assert agent_path.exists(), "AGENT.md should exist after init"
        assert not (project_path / "AGENT.md.template").exists(), "AGENT.md.template should be removed after init"

        agent_content = agent_path.read_text()
        assert "Karpenter" in agent_content, "rendered AGENT.md should mention Karpenter"
        # No jd snippet placeholder may survive; the shipped helm `chart.image` helper ({{...}})
        # is expected to remain (it's a code sample consumers copy), so match only jd tokens.
        leftover = _JD_PLACEHOLDER_RE.findall(agent_content)
        assert not leftover, f"rendered AGENT.md still has unsubstituted jd placeholders: {leftover}"


def test_agent_md_defines_batch_storage_contract(e2e_deployment: EndToEndDeployment) -> None:
    """The rendered guide defines the fixed Helm and KRO batch storage contract."""
    with undeployed_project(e2e_deployment.suite_config) as (project_path, _cli):
        agent_content = (project_path / "AGENT.md").read_text()
        assert "serviceAccountName: batch-inference" in agent_content
        assert "name: batch-storage" in agent_content
        assert "BATCH_INTAKE_BUCKET" in agent_content
        assert "BATCH_OUTPUT_BUCKET" in agent_content


def test_project_is_configurable(e2e_deployment: EndToEndDeployment) -> None:
    """`jd config` succeeds on a freshly scaffolded project.

    For terraform templates this exercises `terraform init`/validate against the
    engine, catching wiring errors without provisioning real infrastructure.
    """
    with undeployed_project(e2e_deployment.suite_config) as (project_path, cli):
        e2e_deployment.configure_project(cli=cli)
        assert (project_path / "engine").exists(), "engine/ should exist after config"
