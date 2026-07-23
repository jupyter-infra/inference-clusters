"""Tests that the template manifest and variables files are well-formed."""

import unittest
from enum import StrEnum
from pathlib import Path
from typing import Any

import hcl2
import yaml
from jupyter_deploy.handlers import base_project_handler

from inference_tf_aws_eks_karpenter.template import TEMPLATE_PATH


class InferenceKarpenterComponentType(StrEnum):
    """The health `components:` types this template declares (jd derives the backing
    `component.<type>.<verb>` cmd from the type)."""

    DEPLOYMENT = "Deployment"
    DAEMONSET = "DaemonSet"
    STATEFULSET = "StatefulSet"
    HELMRELEASE = "HelmRelease"


class TestManifest(unittest.TestCase):
    MANIFEST_PATH: Path = TEMPLATE_PATH / "manifest.yaml"
    VARIABLES_PATH: Path = TEMPLATE_PATH / "variables.yaml"
    MANIFEST: dict[str, Any] | None = None
    VARIABLES_CONFIG: dict[str, Any] | None = None
    EXPECTED_REQUIREMENTS = ["terraform", "awscli", "kubectl"]
    EXPECTED_VALUES = [
        "deployment_id",
        "aws_region",
        "cluster_name",
        "cluster_endpoint",
        "cluster_ca_certificate",
        "kubeconfig_path",
    ]
    EXPECTED_COMMANDS = ["cluster.login", "cluster.status", "cluster.show", "host.list"]
    # jd health wiring: the components/images layers + their backing commands. jd derives a
    # cmd name from `component.<type>.<verb>` / `image.<verb>`, so every verb a component
    # declares needs its matching cmd block below.
    EXPECTED_HEALTH_COMMANDS = [
        # component status (one per type)
        "component.deployment.status",
        "component.daemonset.status",
        "component.statefulset.status",
        "component.helmrelease.status",
        # component show (one per type)
        "component.deployment.show",
        "component.daemonset.show",
        "component.statefulset.show",
        "component.helmrelease.show",
        # Deployment logs+restart, HelmRelease reconcile
        "component.deployment.logs",
        "component.deployment.restart",
        "component.helmrelease.reconcile",
        # images
        "image.status",
        "image.show",
        "image.tags",
        "image.vulnerabilities",
    ]
    # The verbs each component type declares -> the api-name method the verb must map to.
    # A verb here requires both the per-component verb entry AND a matching cmd block.
    VERBS_BY_TYPE = {
        InferenceKarpenterComponentType.DEPLOYMENT: {
            "status": "k8s.apps.get-deployment-status",
            "show": "k8s.apps.get-deployment",
            "logs": "k8s.core.deployment-logs",
            "restart": "k8s.apps.rollout-restart",
        },
        InferenceKarpenterComponentType.DAEMONSET: {
            "status": "k8s.apps.get-daemonset-status",
            "show": "k8s.apps.get-daemonset",
        },
        InferenceKarpenterComponentType.STATEFULSET: {
            "status": "k8s.apps.get-statefulset-status",
            "show": "k8s.apps.get-statefulset",
        },
        InferenceKarpenterComponentType.HELMRELEASE: {
            "status": "helm.status",
            "show": "helm.show",
            "reconcile": "helm.reconcile",
        },
    }
    # component -> declared type. The type binds each component to its full verb set
    # (VERBS_BY_TYPE) and the matching component.<type>.<verb> command blocks.
    EXPECTED_COMPONENTS = {
        "karpenter": InferenceKarpenterComponentType.DEPLOYMENT,
        "keda-operator": InferenceKarpenterComponentType.DEPLOYMENT,
        "keda-metrics-apiserver": InferenceKarpenterComponentType.DEPLOYMENT,
        "keda-admission-webhooks": InferenceKarpenterComponentType.DEPLOYMENT,
        "prometheus-operator": InferenceKarpenterComponentType.DEPLOYMENT,
        "grafana": InferenceKarpenterComponentType.DEPLOYMENT,
        "kube-state-metrics": InferenceKarpenterComponentType.DEPLOYMENT,
        "kro": InferenceKarpenterComponentType.DEPLOYMENT,
        "prometheus": InferenceKarpenterComponentType.STATEFULSET,
        "alertmanager": InferenceKarpenterComponentType.STATEFULSET,
        "node-exporter": InferenceKarpenterComponentType.DAEMONSET,
        "dcgm-exporter": InferenceKarpenterComponentType.DAEMONSET,
        "nvidia-device-plugin": InferenceKarpenterComponentType.DAEMONSET,
        "dcgm-exporter-chart": InferenceKarpenterComponentType.HELMRELEASE,
        "nvidia-device-plugin-chart": InferenceKarpenterComponentType.HELMRELEASE,
    }
    EXPECTED_IMAGES = [
        "keda-operator",
        "keda-metrics-apiserver",
        "keda-admission-webhooks",
        "grafana",
        "dcgm-exporter",
        "device-plugin",
    ]

    @classmethod
    def setUpClass(cls) -> None:
        with open(cls.MANIFEST_PATH) as f:
            cls.MANIFEST = yaml.safe_load(f)
        with open(cls.VARIABLES_PATH) as f:
            cls.VARIABLES_CONFIG = yaml.safe_load(f)

    def test_manifest_parses_as_a_dict(self) -> None:
        self.assertIsInstance(self.MANIFEST, dict)

    def test_manifest_parsable_by_jd(self) -> None:
        manifest = base_project_handler.retrieve_project_manifest(self.MANIFEST_PATH)
        self.assertIsNotNone(manifest)

    def test_all_expected_requirements_declared(self) -> None:
        assert self.MANIFEST is not None
        requirement_names = [req.get("name") for req in self.MANIFEST.get("requirements", [])]
        for expected in self.EXPECTED_REQUIREMENTS:
            self.assertIn(expected, requirement_names)

    def test_all_expected_values_declared(self) -> None:
        assert self.MANIFEST is not None
        value_names = [val.get("name") for val in self.MANIFEST.get("values", [])]
        for expected in self.EXPECTED_VALUES:
            self.assertIn(expected, value_names)

    def test_all_expected_commands_declared(self) -> None:
        assert self.MANIFEST is not None
        command_names = [cmd.get("cmd") for cmd in self.MANIFEST.get("commands", [])]
        for expected in self.EXPECTED_COMMANDS:
            self.assertIn(expected, command_names)

    def test_engine_is_terraform(self) -> None:
        assert self.MANIFEST is not None
        self.assertEqual(self.MANIFEST["template"]["engine"], "terraform")

    def test_health_is_active(self) -> None:
        assert self.MANIFEST is not None
        self.assertEqual(self.MANIFEST.get("health", {}).get("active"), True)

    def test_health_commands_declared(self) -> None:
        assert self.MANIFEST is not None
        command_names = [cmd.get("cmd") for cmd in self.MANIFEST.get("commands", [])]
        for expected in self.EXPECTED_HEALTH_COMMANDS:
            self.assertIn(expected, command_names)

    def test_health_components_declared(self) -> None:
        assert self.MANIFEST is not None
        components = self.MANIFEST.get("components", {})
        for expected, expected_type in self.EXPECTED_COMPONENTS.items():
            self.assertIn(expected, components)
            self.assertEqual(components[expected]["type"], expected_type, f"{expected} must be a {expected_type}")
        # Every component declares exactly its type's verb set, each mapped to the right api.
        for name, comp in components.items():
            comp_type = comp["type"]
            self.assertIn(comp_type, self.VERBS_BY_TYPE, f"{name} has unknown type {comp_type}")
            expected_verbs = self.VERBS_BY_TYPE[comp_type]
            self.assertEqual(set(comp["verbs"]), set(expected_verbs), f"{name} ({comp_type}) verb set mismatch")
            for verb, method in expected_verbs.items():
                self.assertEqual(comp["verbs"][verb]["method"], method, f"{name} ({comp_type}) {verb} method mismatch")

    def test_health_images_declared(self) -> None:
        assert self.MANIFEST is not None
        images = self.MANIFEST.get("images", {})
        for expected in self.EXPECTED_IMAGES:
            self.assertIn(expected, images)
        for name, img in images.items():
            self.assertIn("repository-output", img, f"{name} needs a repository-output")
            self.assertIn("tag-output", img, f"{name} needs a tag-output")

    def test_health_component_scopes_and_images_resolve_to_outputs(self) -> None:
        """Every component `scope` and image `repository-output`/`tag-output` must name a real
        `output` block in engine/outputs.tf. jd's component/image handlers resolve these from
        the FULL terraform output set (get_full_project_outputs) — NOT from the manifest
        `values:` block — so they need no `values:` entry, only the output itself."""
        assert self.MANIFEST is not None
        with open(TEMPLATE_PATH / "engine" / "outputs.tf") as f:
            declared_outputs = {name.strip('"') for block in hcl2.load(f).get("output", []) for name in block}
        for name, comp in self.MANIFEST.get("components", {}).items():
            self.assertIn(comp["scope"], declared_outputs, f"{name} scope {comp['scope']} missing from outputs.tf")
        for name, img in self.MANIFEST.get("images", {}).items():
            self.assertIn(
                img["repository-output"], declared_outputs, f"{name} repository-output missing from outputs.tf"
            )
            self.assertIn(img["tag-output"], declared_outputs, f"{name} tag-output missing from outputs.tf")

    def test_health_parses_into_jd_objects(self) -> None:
        """jd must parse health/components/images into the objects the CLI health handler
        uses — not just accept the file. Guards a schema drift that raw-dict checks miss."""
        manifest = base_project_handler.retrieve_project_manifest(self.MANIFEST_PATH)
        self.assertIsNotNone(manifest.health)
        assert manifest.health is not None
        self.assertTrue(manifest.health.active)
        self.assertEqual(set(manifest.get_components()), set(self.EXPECTED_COMPONENTS))
        self.assertEqual(set(manifest.get_images()), set(self.EXPECTED_IMAGES))
        for cmd in self.EXPECTED_HEALTH_COMMANDS:
            self.assertTrue(manifest.has_command(cmd), f"jd must expose command {cmd}")

    def test_value_source_keys_are_real_terraform_outputs(self) -> None:
        """Every declared `values:` entry (source: output) must map to an actual `output`
        block in engine/outputs.tf — the cross-file link that would silently break the jd
        commands/well-known keys (region, cluster_name, ...) at runtime if an output were
        renamed. Component scopes + image repo/tag are covered separately (they bypass
        `values:` and resolve from the full output set)."""
        assert self.MANIFEST is not None
        with open(TEMPLATE_PATH / "engine" / "outputs.tf") as f:
            declared_outputs = {name.strip('"') for block in hcl2.load(f).get("output", []) for name in block}

        for val in self.MANIFEST.get("values", []):
            if val.get("source") != "output":
                continue
            source_key = val.get("source-key", val["name"])
            self.assertIn(
                source_key,
                declared_outputs,
                f"value '{val['name']}' -> output '{source_key}' missing from outputs.tf",
            )

    def test_variables_parses_as_a_dict(self) -> None:
        self.assertIsInstance(self.VARIABLES_CONFIG, dict)

    def test_variables_config_has_overrides_key(self) -> None:
        assert self.VARIABLES_CONFIG is not None
        # variables.yaml overrides are commented out in the seed; this guards the schema shape.
        self.assertIn("overrides", self.VARIABLES_CONFIG)

    def test_preset_defaults_are_declared_variables(self) -> None:
        """Every key in defaults-all.tfvars must have a matching variable block in variables.tf."""
        engine = TEMPLATE_PATH / "engine"
        with open(engine / "presets" / "defaults-all.tfvars") as f:
            # hcl2 surfaces comments under a "__comments__" pseudo-key; drop it.
            preset_keys = set(hcl2.load(f).keys()) - {"__comments__"}
        with open(engine / "variables.tf") as f:
            # hcl2 v7 keeps the block label quoted (e.g. '"region"'); strip the quotes.
            declared = {name.strip('"') for block in hcl2.load(f).get("variable", []) for name in block}
        undeclared = preset_keys - declared
        self.assertEqual(undeclared, set(), f"undeclared preset keys: {undeclared}")
