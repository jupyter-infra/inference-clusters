"""Contract tests for the opt-in SageMaker HyperPod Inference Gateway.

Scope: only the invariants `terraform validate` cannot catch and whose drift is silent —
the opt-in gate, add-on configuration keys that collide with the base template, the
IRSA trust subjects the add-on's fixed service accounts must match, the TLS-bucket name
the managed policy is scoped to, the scheduler-label contract shared by Terraform, the
gateway chart and the KRO starter, and the chart's rendered InferenceGatewayConfig shape.
Observable behavior (add-on ACTIVE, gateway Ready, requests + metrics through the gateway)
is covered by tests/e2e/test_hyperpod_inference_gateway.py.
"""

import re
import subprocess
import unittest
from pathlib import Path
from typing import Any

import hcl2
import yaml

from inference_tf_aws_eks_karpenter.template import TEMPLATE_PATH

ENGINE = TEMPLATE_PATH / "engine"
CHARTS = TEMPLATE_PATH / "charts"
GATEWAY_TF = ENGINE / "platform_hyperpod_gateway.tf"
GATEWAY_CHART = CHARTS / "hyperpod-gateway"

# Fixed by the add-on (not configurable): the namespace and service accounts its
# components run as. The IRSA trust policies MUST name exactly these.
ADDON_NAMESPACE = "hyperpod-inference-system"
OPERATOR_SA = "hyperpod-inference-controller-manager"
GATEWAY_CONTROLLER_SA = "inference-gateway-controller"
# First add-on version that ships the Inference Gateway (AWS release notes).
MIN_GATEWAY_ADDON_VERSION = (2, 0, 0, 2)


def _block(content: str, header_re: str) -> str:
    """Brace-matched body of the first block whose header matches `header_re`."""
    start = re.search(header_re + r"\s*\{", content)
    assert start is not None, f"block {header_re!r} not found"
    depth, idx = 1, start.end()
    while idx < len(content) and depth > 0:
        depth += {"{": 1, "}": -1}.get(content[idx], 0)
        idx += 1
    return content[start.end() : idx - 1]


def _resource(content: str, type_: str, name: str) -> str:
    return _block(content, rf'resource\s+"{re.escape(type_)}"\s+"{re.escape(name)}"')


def _unquote(value: Any) -> Any:
    """python-hcl2 keeps string literals quoted ('"x"'); strip that recursively."""
    if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    if isinstance(value, list):
        return [_unquote(v) for v in value]
    if isinstance(value, dict):
        return {_unquote(k): _unquote(v) for k, v in value.items()}
    return value


def _defaults() -> dict[str, Any]:
    with open(ENGINE / "presets" / "defaults-all.tfvars") as f:
        return dict(_unquote(hcl2.load(f)))


def _tf_local(name: str) -> str:
    """The string literal assigned to `local.<name>` anywhere in the engine."""
    for tf in ENGINE.glob("*.tf"):
        m = re.search(rf'^\s*{re.escape(name)}\s*=\s*"([^"]*)"', tf.read_text(), re.MULTILINE)
        if m:
            return m.group(1)
    raise AssertionError(f"local {name} not found")


def _set_args(sets: tuple[str, ...]) -> list[str]:
    """`--set k=v` per entry, or `--set-json k=<json>` when the entry starts with `json:`."""
    args: list[str] = []
    for s in sets:
        args += ["--set-json", s.removeprefix("json:")] if s.startswith("json:") else ["--set", s]
    return args


def _render(*sets: str) -> list[dict]:
    """helm template the gateway chart (namespace `inference`) and return its YAML docs."""
    args = _set_args(sets)
    out = subprocess.run(
        ["helm", "template", "t", str(GATEWAY_CHART), "-n", "inference", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _render_fails(*sets: str) -> str:
    args = _set_args(sets)
    r = subprocess.run(
        ["helm", "template", "t", str(GATEWAY_CHART), *args], check=False, capture_output=True, text=True
    )
    assert r.returncode != 0, "render was expected to fail"
    return r.stderr


class TestGatewayIsOptIn(unittest.TestCase):
    def test_every_gateway_resource_is_gated(self) -> None:
        """Every resource / module / data source in platform_hyperpod_gateway.tf is gated on the
        opt-in flag (count or for_each), so a default cluster gets none of it (no OIDC provider,
        no IAM roles, no endpoints billed, no add-on)."""
        content = GATEWAY_TF.read_text()
        headers = list(re.finditer(r'^(resource|data)\s+"([\w-]+)"\s+"([\w-]+)"|^module\s+"([\w-]+)"', content, re.M))
        self.assertTrue(headers, "no blocks declared in platform_hyperpod_gateway.tf")
        for m in headers:
            body = _block(content[m.start() :], re.escape(m.group(0)))
            name = m.group(0)
            gated = re.search(
                r"count\s*=\s*local\.hyperpod_gw_enabled(\s*&&[^?]+)?\s*\?\s*1\s*:\s*0", body
            ) or re.search(r"for_each\s*=\s*local\.hyperpod_gateway_endpoints", body)
            self.assertTrue(gated, f"{name} is not gated on enable_hyperpod_inference_gateway")
        self.assertIn("local.hyperpod_gw_enabled ? toset(", content, "endpoint set must be empty when disabled")
        self.assertRegex(content, r"hyperpod_gw_enabled\s*=\s*var\.enable_hyperpod_inference_gateway")

    def test_default_preset_is_off_with_a_gateway_capable_version(self) -> None:
        defaults = _defaults()
        self.assertIs(defaults["enable_hyperpod_inference_gateway"], False)
        self.assertEqual(defaults["hyperpod_cluster_arn"], "", "plain-EKS default: no HyperPod ARN")
        m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)-eksbuild\.(\d+)", defaults["hyperpod_inference_addon_version"])
        assert m is not None, "add-on version must be an EKS add-on version string"
        self.assertGreaterEqual(
            tuple(int(g) for g in m.groups()),
            MIN_GATEWAY_ADDON_VERSION,
            "the Inference Gateway ships from v2.0.0-eksbuild.2",
        )
        schedulers = defaults["hyperpod_inference_gateway_schedulers"]
        self.assertGreaterEqual(len(schedulers), 1, "InferenceGatewayConfig requires >= 1 scheduler")

    def test_mutually_exclusive_with_inference_routing_crds(self) -> None:
        """Both paths install the InferencePool CRDs; a precondition must refuse the combination."""
        addon = _resource(GATEWAY_TF.read_text(), "aws_eks_addon", "hyperpod_inference")
        self.assertRegex(addon, r"precondition\s*\{[^}]*condition\s*=\s*!var\.enable_inference_routing")


class TestAddonConfiguration(unittest.TestCase):
    ADDON = _resource(GATEWAY_TF.read_text(), "aws_eks_addon", "hyperpod_inference")

    def test_both_components_enabled_and_required_keys_set(self) -> None:
        self.assertIn('addon_name    = "amazon-sagemaker-hyperpod-inference"', self.ADDON)
        self.assertIn("addon_version = var.hyperpod_inference_addon_version", self.ADDON)
        self.assertRegex(self.ADDON, r"inferenceOperator\s*=\s*\{\s*enabled\s*=\s*true\s*\}")
        self.assertRegex(self.ADDON, r"inferenceGateway\s*=\s*\{\s*enabled\s*=\s*true")
        # Required by the add-on configuration schema.
        self.assertIn("executionRoleArn", self.ADDON)
        self.assertIn("tlsCertificateS3Bucket", self.ADDON)

    def test_bundled_keda_disabled_and_alb_wired_to_its_irsa_role(self) -> None:
        """The add-on bundles KEDA, on by default; the template already runs KEDA
        (platform_keda.tf), and a second one fights over the keda.sh CRDs and the
        external-metrics APIService. The bundled ALB controller must stay ON: the operator's
        init check hard-requires the elbv2.k8s.aws CRDs (verified live)."""
        self.assertRegex(self.ADDON, r"keda\s*=\s*\{\s*enabled\s*=\s*false\s*\}")
        alb = _block(self.ADDON, r"alb\s*=")
        self.assertRegex(alb, r"enabled\s*=\s*true")
        self.assertIn("module.hyperpod_alb_controller_role[0].role_arn", alb)

    def test_operator_dependency_check_is_satisfied(self) -> None:
        """The operator init container requires CSIDriver fsx.csi.aws.com: installed by the
        aws-fsx-csi-driver add-on unless enable_fsx's Helm release already provides it
        (never both — they would collide on the CSIDriver object)."""
        content = GATEWAY_TF.read_text()
        fsx = _resource(content, "aws_eks_addon", "hyperpod_fsx_csi_driver")
        self.assertRegex(fsx, r"count\s*=\s*local\.hyperpod_gw_enabled\s*&&\s*!var\.enable_fsx\s*\?\s*1\s*:\s*0")
        self.assertIn('addon_name   = "aws-fsx-csi-driver"', fsx)
        deps = re.search(r"depends_on\s*=\s*\[(.*?)\]", self.ADDON, re.S)
        assert deps is not None
        for dep in (
            "aws_eks_addon.hyperpod_fsx_csi_driver",
            "helm_release.fsx_csi_driver",
            "aws_eks_addon.cert_manager",
        ):
            self.assertIn(dep, deps.group(1))

    def test_hyperpod_cluster_arn_only_passed_when_set(self) -> None:
        """hyperpodClusterArn is optional in the schema; an empty string would fail its ARN pattern."""
        self.assertRegex(
            self.ADDON,
            r'var\.hyperpod_cluster_arn\s*==\s*""\s*\?\s*\{\}\s*:\s*\{\s*hyperpodClusterArn\s*=\s*var\.hyperpod_cluster_arn',
        )

    def test_addon_ordered_after_dependencies(self) -> None:
        deps = re.search(r"depends_on\s*=\s*\[(.*?)\]", self.ADDON, re.S)
        assert deps is not None
        for dep in ("null_resource.cluster_addons", "aws_eks_addon.cert_manager", "aws_vpc_endpoint.hyperpod_gateway"):
            self.assertIn(dep, deps.group(1), f"add-on must depend_on {dep}")
        release = _resource(GATEWAY_TF.read_text(), "helm_release", "hyperpod_gateway")
        self.assertIn(
            "aws_eks_addon.hyperpod_inference", release, "IGC must install after (and uninstall before) its CRD"
        )


class TestAddonIam(unittest.TestCase):
    CONTENT = GATEWAY_TF.read_text()

    def test_irsa_trust_subjects_match_the_addon_service_accounts(self) -> None:
        self.assertEqual(_tf_local("hyperpod_inference_namespace"), ADDON_NAMESPACE)
        self.assertEqual(_tf_local("hyperpod_operator_sa"), OPERATOR_SA)
        self.assertEqual(_tf_local("hyperpod_gateway_controller_sa"), GATEWAY_CONTROLLER_SA)
        self.assertEqual(_tf_local("hyperpod_alb_controller_sa"), "aws-load-balancer-controller")
        for doc, sa_local in (
            ("hyperpod_operator_trust", "hyperpod_operator_sa"),
            ("hyperpod_gateway_controller_trust", "hyperpod_gateway_controller_sa"),
            ("hyperpod_alb_controller_trust", "hyperpod_alb_controller_sa"),
        ):
            body = _block(self.CONTENT, rf'data\s+"aws_iam_policy_document"\s+"{doc}"')
            self.assertIn("sts:AssumeRoleWithWebIdentity", body)
            self.assertIn("aws_iam_openid_connect_provider.eks[0].arn", body)
            self.assertIn(f"system:serviceaccount:${{local.hyperpod_inference_namespace}}:${{local.{sa_local}}}", body)
            self.assertIn(":aud", body)

    def test_roles_use_the_managed_policies_and_postfixed_names(self) -> None:
        for module, policy in (
            ("hyperpod_operator_role", "AmazonSageMakerHyperPodInferenceAccess"),
            ("hyperpod_gateway_controller_role", "AmazonSageMakerHyperPodInferenceGatewayAccess"),
        ):
            body = _block(self.CONTENT, rf'module\s+"{module}"')
            self.assertIn(policy, body)
            self.assertIn("${local.resource_name_prefix}", body, f"{module} name must embed the postfix")

    def test_operator_may_pass_only_its_own_role_to_sagemaker(self) -> None:
        """At start-up the operator calls sagemaker:UpdateClusterInference passing its own
        execution role; without iam:PassRole it crash-loops (verified live). The grant must be
        scoped to that one role and to SageMaker as the receiving service."""
        doc = _block(self.CONTENT, r'data\s+"aws_iam_policy_document"\s+"hyperpod_operator_pass_role"')
        self.assertRegex(doc, r'actions\s*=\s*\["iam:PassRole"\]')
        self.assertRegex(doc, r"resources\s*=\s*\[module\.hyperpod_operator_role\[0\]\.role_arn\]")
        self.assertIn('variable = "iam:PassedToService"', doc)
        self.assertIn('values   = ["sagemaker.amazonaws.com"]', doc)
        policy = _resource(self.CONTENT, "aws_iam_role_policy", "hyperpod_operator_pass_role")
        self.assertIn("module.hyperpod_operator_role[0].role_name", policy)
        addon = _resource(self.CONTENT, "aws_eks_addon", "hyperpod_inference")
        self.assertIn(
            "aws_iam_role_policy.hyperpod_operator_pass_role", addon, "grant must exist before the operator starts"
        )

    def test_tls_bucket_is_in_the_managed_policy_scope(self) -> None:
        """AmazonSageMakerHyperPodInferenceAccess grants S3 only on `hyperpod-tls*` buckets, and the
        add-on schema requires `tls` in the name; the random_id postfix keeps two deployments apart."""
        body = _block(self.CONTENT, r'module\s+"hyperpod_tls_bucket"')
        m = re.search(r'bucket_name_prefix\s*=\s*"([^"]+)"', body)
        assert m is not None
        self.assertTrue(m.group(1).startswith("hyperpod-tls-"), m.group(1))
        self.assertIn("${random_id.postfix.hex}", m.group(1))

    def test_endpoints_only_vpc_gets_sagemaker_and_acm_endpoints(self) -> None:
        m = re.search(r"hyperpod_gateway_endpoints\s*=.*?toset\(\[(.*?)\]\)", self.CONTENT)
        assert m is not None
        self.assertEqual(set(re.findall(r'"([^"]+)"', m.group(1))), {"sagemaker.api", "acm"})
        endpoint = _resource(self.CONTENT, "aws_vpc_endpoint", "hyperpod_gateway")
        self.assertIn("private_dns_enabled = true", endpoint)
        self.assertIn('vpc_endpoint_type   = "Interface"', endpoint)


class TestSchedulerLabelContract(unittest.TestCase):
    """One label key ties Terraform, the gateway chart's modelSelector and the KRO starter's pod
    template together; if any copy drifts, pods silently never join an InferencePool."""

    def test_label_key_agrees_everywhere(self) -> None:
        label = _tf_local("hyperpod_gateway_scheduler_label")
        gw_values = yaml.safe_load((GATEWAY_CHART / "values.yaml").read_text())
        kro_values = yaml.safe_load((CHARTS / "kro" / "values.yaml").read_text())
        self.assertEqual(gw_values["schedulerLabel"], label)
        self.assertEqual(kro_values["gatewaySchedulerLabel"], label)
        kro_release = _resource((ENGINE / "platform_kro.tf").read_text(), "helm_release", "kro_starters")
        self.assertIn("local.hyperpod_gateway_scheduler_label", kro_release)
        gw_release = _resource(GATEWAY_TF.read_text(), "helm_release", "hyperpod_gateway")
        self.assertIn("schedulerLabel = local.hyperpod_gateway_scheduler_label", gw_release)

    def test_kro_starter_stamps_the_label_from_its_schema(self) -> None:
        rgd_out = subprocess.run(
            ["helm", "template", "t", str(CHARTS / "kro")], check=True, capture_output=True, text=True
        ).stdout
        rgd = next(d for d in yaml.safe_load_all(rgd_out) if d and d["metadata"]["name"] == "inference-deployment")
        self.assertIn("gatewayScheduler", rgd["spec"]["schema"]["spec"])
        deployment = next(r for r in rgd["spec"]["resources"] if r["id"] == "deployment")
        labels = deployment["template"]["spec"]["template"]["metadata"]["labels"]
        self.assertEqual(labels["inference/gateway-scheduler"], "${schema.spec.gatewayScheduler}")

    def test_terraform_scheduler_fields_map_onto_chart_values(self) -> None:
        """The helm_release translates the tf object (snake_case) to the chart's camelCase keys."""
        release = _resource(GATEWAY_TF.read_text(), "helm_release", "hyperpod_gateway")
        for chart_key, tf_attr in (
            ("name", 's["name"]'),
            ("modelName", 's["model_name"]'),
            ("targetPort", 'tonumber(s["target_port"])'),
        ):
            self.assertRegex(release, rf"{chart_key}\s*=\s*{re.escape(tf_attr)}")
        chart_scheduler = yaml.safe_load((GATEWAY_CHART / "values.yaml").read_text())["schedulers"][0]
        self.assertEqual(set(chart_scheduler), {"name", "modelName", "targetPort"})


class TestGatewayChartRender(unittest.TestCase):
    def _igc(self, docs: list[dict]) -> dict:
        igcs = [d for d in docs if d["kind"] == "InferenceGatewayConfig"]
        self.assertEqual(len(igcs), 1, "exactly one default InferenceGatewayConfig")
        return igcs[0]

    def test_default_render_is_a_valid_gateway_config(self) -> None:
        igc = self._igc(_render())
        self.assertEqual(igc["apiVersion"], "inference.sagemaker.aws.amazon.com/v1alpha1")
        self.assertEqual(igc["metadata"]["namespace"], "inference", "IGC lives with its model pods")
        self.assertIs(igc["spec"]["bbr"]["enabled"], True)
        (sched,) = igc["spec"]["schedulers"]
        self.assertEqual(sched["scheduler"], "llm-d")
        self.assertEqual(sched["modelSelector"], {"matchLabels": {"inference/gateway-scheduler": sched["name"]}})
        self.assertIsInstance(sched["targetPort"], int)

    def test_multiple_models_render_one_scheduler_each(self) -> None:
        igc = self._igc(
            _render(
                'json:schedulers=[{"name":"a","modelName":"org/model-a","targetPort":8000},'
                '{"name":"b","modelName":"org/model-b","targetPort":9000}]'
            )
        )
        by_name = {s["name"]: s for s in igc["spec"]["schedulers"]}
        self.assertEqual(by_name["a"]["modelName"], "org/model-a")
        self.assertEqual(by_name["b"]["targetPort"], 9000)
        self.assertIs(igc["spec"]["bbr"]["enabled"], True, "BBR is mandatory with >1 scheduler")

    def test_invalid_scheduler_sets_fail_at_render(self) -> None:
        dup = (
            'json:schedulers=[{"name":"a","modelName":"m","targetPort":8000},'
            '{"name":"b","modelName":"m","targetPort":8000}]'
        )
        self.assertIn("duplicate scheduler modelName", _render_fails(dup))
        self.assertIn("at least one scheduler", _render_fails("json:schedulers=[]"))

    def test_scrape_objects_are_selected_by_prometheus(self) -> None:
        docs = _render()
        monitors = [d for d in docs if d["kind"] == "PodMonitor"]
        self.assertEqual(len(monitors), 2, "one PodMonitor for the EPPs, one for the BBR")
        for pm in monitors:
            self.assertEqual(pm["metadata"]["labels"].get("release"), "kube-prometheus-stack")
        self.assertEqual(
            sorted(d["kind"] for d in _render("scrape.enabled=false")), ["InferenceGatewayConfig", "NetworkPolicy"]
        )

    def test_epp_admits_envoy_and_prometheus_through_network_policy(self) -> None:
        """The add-on's own EPP NetworkPolicy only admits same-namespace callers, but Envoy runs
        in the add-on namespace and Prometheus in monitoring: with policy enforcement on (this
        template), every gateway request 500s on ext-proc timeout without this (verified live)."""
        (netpol,) = [d for d in _render() if d["kind"] == "NetworkPolicy"]
        self.assertEqual(
            netpol["spec"]["podSelector"],
            {"matchExpressions": [{"key": "inference.sagemaker.aws.amazon.com/scheduler", "operator": "Exists"}]},
        )
        admitted = {
            (rule["from"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"], p["port"])
            for rule in netpol["spec"]["ingress"]
            for p in rule["ports"]
        }
        self.assertEqual(admitted, {("hyperpod-inference-system", 9002), ("monitoring", 9090)})


class TestGatewayPodsFitTheSystemNodeGroup(unittest.TestCase):
    def test_epp_and_bbr_are_pinned_and_sized(self) -> None:
        """The controller's EPP default (4 CPU / 8Gi x2 per scheduler) cannot schedule on the
        m6i.xlarge system NG (verified live: Pending), so podDefaults must pin AND size them."""
        release = _resource(GATEWAY_TF.read_text(), "helm_release", "hyperpod_gateway")
        pod_defaults = _block(release, r"podDefaults\s*=")
        self.assertIn("local.system_node_selector", pod_defaults)
        self.assertIn("local.system_toleration", pod_defaults)
        self.assertRegex(pod_defaults, r"requests\s*=\s*\{\s*cpu\s*=")
        self.assertRegex(pod_defaults, r"limits\s*=\s*\{\s*memory\s*=")

    def test_igc_is_rendered_with_mandatory_tls(self) -> None:
        """spec.tls is required by the CRD (no HTTP-only mode); omitting it is rejected at admission."""
        igc = next(d for d in _render() if d["kind"] == "InferenceGatewayConfig")
        self.assertEqual(igc["spec"]["tls"], {}, "default = auto-issue")
        byo = next(
            d
            for d in _render("tls.acmArn=arn:aws:acm:us-east-1:123456789012:certificate/abc")
            if d["kind"] == "InferenceGatewayConfig"
        )
        self.assertEqual(byo["spec"]["tls"], {"acmArn": "arn:aws:acm:us-east-1:123456789012:certificate/abc"})


class TestGatewayChartVersioned(unittest.TestCase):
    def test_chart_tracks_template_version_and_content_hash(self) -> None:
        template_version = yaml.safe_load((TEMPLATE_PATH / "manifest.yaml").read_text())["template"]["version"]
        chart = yaml.safe_load((GATEWAY_CHART / "Chart.yaml").read_text())
        self.assertEqual(chart["version"], template_version.replace("rc", "-rc"))
        script = (Path(__file__).parents[4] / "scripts" / "upgrade_template_version.py").read_text()
        self.assertRegex(script, r'synced_charts=\([^)]*"hyperpod-gateway"', "version bumps must carry this chart")
        # Editing a chart file must produce a plan diff (see main.tf chart_hashes).
        self.assertRegex((ENGINE / "main.tf").read_text(), r'chart_dirs\s*=\s*\[[^\]]*"hyperpod-gateway"')
        release = _resource(GATEWAY_TF.read_text(), "helm_release", "hyperpod_gateway")
        self.assertIn('local.chart_hashes["hyperpod-gateway"]', release)
