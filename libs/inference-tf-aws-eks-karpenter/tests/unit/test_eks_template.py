"""HCL structure + wiring assertions for the engine.

Scope (deliberately narrow — mirrors the eks-oidc template test): this guards only
invariants where drift is BOTH silent AND costly — load-bearing depends_on / destroy
ordering, air-gap image sourcing (pull-through vs vendored), security-scoped IAM,
control-loop placement + HA, and cost-safety of the gated GPU pool. It does NOT snapshot
arbitrary resource bodies, docs, or decorative wiring; those change often and a test that
merely mirrors them is churn, not a guard. Parsing is regex + brace-matching (no hcl2).
"""

import re

import yaml

from inference_tf_aws_eks_karpenter.template import TEMPLATE_PATH

ENGINE = TEMPLATE_PATH / "engine"
CHARTS = TEMPLATE_PATH / "charts"


def _extract_block(content: str, kind: str, type_: str, name: str) -> str:
    """Body of a `<kind> "<type>" "<name>" { ... }` block (kind = resource|data), brace-matched."""
    start = re.search(rf'{kind}\s+"{re.escape(type_)}"\s+"{re.escape(name)}"\s*\{{', content)
    assert start is not None, f"{kind} {type_}.{name} not found"
    depth, idx = 1, start.end()
    while idx < len(content) and depth > 0:
        depth += {"{": 1, "}": -1}.get(content[idx], 0)
        idx += 1
    return content[start.end() : idx - 1]


def _resource(content: str, type_: str, name: str) -> str:
    return _extract_block(content, "resource", type_, name)


def _depends_on(block: str, resource_type: str) -> set[str]:
    """Set of `<resource_type>` names referenced in a depends_on list."""
    match = re.search(r"depends_on\s*=\s*\[(.*?)\]", block, re.DOTALL)
    assert match is not None, "no depends_on block found"
    return set(re.findall(rf"{re.escape(resource_type)}\.(\w+)", match.group(1)))


# --- Version consistency (single source of truth = manifest.yaml) ---


def test_local_chart_versions_match_template_version() -> None:
    """Every first-party chart's Chart.yaml version tracks the template version (SemVer spelling)."""
    template_version = yaml.safe_load((TEMPLATE_PATH / "manifest.yaml").read_text())["template"]["version"]
    semver = template_version.replace("rc", "-rc")  # PEP 440 0.1.0rc1 == SemVer 0.1.0-rc1
    for chart in ("karpenter", "kro"):
        version = yaml.safe_load((CHARTS / chart / "Chart.yaml").read_text())["version"]
        assert version == semver, f"charts/{chart} version ({version}) must equal template SemVer ({semver})"


# --- Load-bearing depends_on / destroy ordering (silent + catastrophic if dropped) ---


def test_all_eks_addons_gated_by_cluster_addons() -> None:
    """Every aws_eks_addon MUST be in null_resource.cluster_addons.depends_on.

    This barrier keeps addons alive until every Helm chart uninstalls; an addon not wired
    into it silently regresses destroy ordering and `jd down` can orphan etcd resources.
    """
    content = (ENGINE / "eks_addons.tf").read_text()
    declared = set(re.findall(r'resource\s+"aws_eks_addon"\s+"(\w+)"', content))
    assert declared, "no aws_eks_addon resources found"
    gated = _depends_on(_resource(content, "null_resource", "cluster_addons"), "aws_eks_addon")
    assert not (declared - gated), f"aws_eks_addon(s) {sorted(declared - gated)} not in cluster_addons.depends_on"


def test_cluster_addons_gates_admin_access_and_node_entry() -> None:
    """cluster_addons MUST depend on the admin access associations + node access entry.

    They authorize the Helm/K8s providers; on destroy they must outlive the charts or
    remaining uninstalls fail "forbidden" (the eks-oidc lesson).
    """
    block = _resource((ENGINE / "eks_addons.tf").read_text(), "null_resource", "cluster_addons")
    assert {"admin_role", "admin_user"} <= _depends_on(block, "aws_eks_access_policy_association")
    assert "node" in _depends_on(block, "aws_eks_access_entry")


def test_core_node_addons_are_daemonsets_only() -> None:
    """core_node_addons must gate ONLY vpc-cni + kube-proxy (a Deployment addon → create-time cycle)."""
    block = _resource((ENGINE / "eks_addons.tf").read_text(), "null_resource", "core_node_addons")
    gated = _depends_on(block, "aws_eks_addon")
    assert gated == {"vpc_cni", "kube_proxy"}, f"core_node_addons should gate vpc_cni + kube_proxy, got {sorted(gated)}"


def test_system_node_group_ordering_and_taint() -> None:
    """The system NG must be tainted+labeled and ordered after CNI/kube-proxy, the node access entry,
    and the pull-through path (else nodes fail to join / boot before their image path exists)."""
    content = (ENGINE / "main.tf").read_text()
    block = re.search(r"module\s+\"node_group\".*?\n\}", content, re.DOTALL)
    assert block is not None, "module.node_group not found"
    block = block.group(0)
    assert '"inference/role" = "system"' in block and "NO_SCHEDULE" in block, "system NG must be labeled + tainted"
    for dep in ("null_resource.core_node_addons", "aws_eks_access_entry.node", "null_resource.pullthrough_ready"):
        assert dep in block, f"module.node_group must depend_on {dep}"


def test_karpenter_drain_ordering() -> None:
    """The drain poller's triggers reference the controller/cluster/access-entry as ATTRIBUTES
    (the load-bearing destroy edges), and the NodePool release deletes BEFORE the drain runs.

    A captured string instead of an attribute ref silently drops the edge → orphaned nodes.
    """
    content = (ENGINE / "platform_karpenter.tf").read_text()
    drain = _resource(content, "null_resource", "karpenter_drain")
    assert "helm_release.karpenter.id" in drain, "drain must reference the controller release attribute"
    assert "module.eks_cluster.cluster_endpoint" in drain and "aws_eks_access_entry" in drain
    assert "when        = destroy" in drain or "when = destroy" in drain.replace("  ", " ")
    nodepools = _resource(content, "helm_release", "karpenter_nodepools")
    assert "null_resource.karpenter_drain" in nodepools and "helm_release.karpenter" in nodepools


# --- Air-gap: pull-through supply + image sourcing ---


def test_trusted_upstreams_are_no_credentials_only() -> None:
    """trusted_upstreams MUST be EXACTLY the three no-credentials pull-through upstreams.

    A credentialed host (docker.io/ghcr.io) would need a Secrets Manager secret we refuse
    to own — it must be vendored instead, never added here.
    """
    block = re.search(r"trusted_upstreams\s*=\s*\{(.*?)\n  \}", (ENGINE / "images.tf").read_text(), re.DOTALL)
    assert block is not None, "trusted_upstreams local not found"
    hosts = set(re.findall(r'url\s*=\s*"([^"]+)"', block.group(1)))
    assert hosts == {"public.ecr.aws", "quay.io", "registry.k8s.io"}, f"got {sorted(hosts)}"


def test_node_role_has_pullthrough_import_permissions() -> None:
    """The node role MUST be granted ecr import-on-miss (the pull-through allowlist)."""
    content = (ENGINE / "images.tf").read_text()
    assert "ecr:BatchImportUpstreamImage" in content and "ecr:CreateRepository" in content
    assert "aws_iam_role_policy" in content and "node_pullthrough" in content


def test_pullthrough_ready_barrier_gates_infra_and_iam() -> None:
    """pullthrough_ready MUST depend on the shared infra + node import IAM; NO redundant registry policy."""
    content = (ENGINE / "images.tf").read_text()
    block = _resource(content, "null_resource", "pullthrough_ready")
    assert "null_resource.pullthrough_infra" in block and "aws_iam_role_policy.node_pullthrough" in block
    assert 'resource "aws_ecr_registry_policy"' not in content, (
        "registry policy is redundant + failed PutRegistryPolicy"
    )


def test_pullthrough_infra_is_shared_singleton_not_tf_resource() -> None:
    """The pull-through rule + creation template are account-regional singletons → NOT TF resources.

    As TF resources they collide across two deployments in one account+region (2nd apply
    AlreadyExists; 1st `jd down` deletes them from under the survivor). Provisioned
    imperatively in pullthrough.tf (create-if-absent / adopt / fail-on-divergence).
    """
    for f in ("images.tf", "pullthrough.tf"):
        content = (ENGINE / f).read_text()
        assert 'resource "aws_ecr_pull_through_cache_rule"' not in content, f"{f}: rule must be imperative"
        assert 'resource "aws_ecr_repository_creation_template"' not in content, f"{f}: template must be imperative"


def test_platform_images_pinned_or_vendored() -> None:
    """Every platform image resolves via pull-through (pinned URI) OR is vendored to ECR — never a
    bare docker.io/ghcr.io pull. Images on a no-creds upstream are pinned; the rest are vendored."""
    images = (ENGINE / "images.tf").read_text()
    # Vendored: no no-creds home (nvcr.io / docker.io / ghcr.io).
    for key, src in (
        ("dcgm_exporter", "nvcr.io/nvidia/k8s/dcgm-exporter"),
        ("grafana", "docker.io/grafana/grafana"),
        ("keda_operator", "ghcr.io/kedacore/keda"),
    ):
        assert key in images and src in images, f"{key} must be a vendored_images entry from {src}"
    assert "skopeo copy --all" not in images, "vendoring must omit --all (SBOM layer breaks skopeo 1.4.1)"

    prom = _resource((ENGINE / "platform_prometheus.tf").read_text(), "helm_release", "kube_prometheus_stack")
    assert "local.quay_registry" in prom and "local.k8s_registry" in prom, "prometheus images must pin pull-through"
    assert 'aws_ecr_repository.vendored["grafana"]' in prom, "Grafana must resolve to the vendored ECR repo"
    code = "\n".join(ln for ln in prom.splitlines() if not ln.lstrip().startswith("#"))
    assert "docker.io" not in code and "ghcr.io" not in code, "no prometheus image may reference docker.io/ghcr.io"

    # registry.k8s.io images pinned to the pull-through URI (KRO chart + controller, CA).
    kro = _resource((ENGINE / "platform_kro.tf").read_text(), "helm_release", "kro")
    assert "registry-k8s/kro/kro" in kro and "oci://registry.k8s.io/kro" in kro
    assert "repository_password" not in kro, "KRO chart pull must be anonymous (perpetual-diff trap)"
    ca = _resource((ENGINE / "platform_cluster_autoscaler.tf").read_text(), "helm_release", "cluster_autoscaler")
    assert "registry-k8s/autoscaling/cluster-autoscaler" in ca, "CA image must pin the registry-k8s pull-through URI"


def test_karpenter_chart_pull_is_unauthenticated() -> None:
    """The Karpenter helm_release MUST NOT set chart-pull auth.

    public.ecr.aws serves the chart anonymously; a minted token → perpetual diff → the
    release UPDATEs every apply → recreated drain poller wipes NodePools; and the token
    goes stale + 403s the refresh. (Diagnosed live 2026-07-04.)
    """
    block = _resource((ENGINE / "platform_karpenter.tf").read_text(), "helm_release", "karpenter")
    assert "repository_password" not in block and "repository_username" not in block
    assert "aws_ecrpublic_authorization_token" not in (ENGINE / "main.tf").read_text()


# --- Security-scoped IAM + air-gap Karpenter specifics ---


def test_karpenter_controller_policy_is_cluster_scoped() -> None:
    """Karpenter controller EC2 create/delete MUST be scoped by the cluster tag, never account-wide."""
    content = (ENGINE / "iam.tf").read_text()
    assert 'data "aws_iam_policy_document" "karpenter_controller"' in content
    assert "kubernetes.io/cluster/" in content, "controller policy not scoped by cluster tag"
    assert "ec2:TerminateInstances" in content and "ec2:RunInstances" in content


def test_nodeclass_uses_precreated_instance_profile_not_role() -> None:
    """EC2NodeClass MUST use a pre-created instanceProfile, never `role`.

    On the endpoints-only VPC Karpenter can't reach IAM, so `role` (self-managed profile)
    hangs the reconcile → every downstream controller misreports "no subnets found".
    """
    iam = (ENGINE / "iam.tf").read_text()
    assert 'resource "aws_iam_instance_profile" "node"' in iam and "iam:CreateInstanceProfile" not in iam
    nodeclass = (CHARTS / "karpenter" / "templates" / "ec2nodeclass.yaml").read_text()
    assert "instanceProfile:" in nodeclass and not re.search(r"^\s*role:", nodeclass, re.MULTILINE)
    assert "aws_iam_instance_profile.node.name" in (ENGINE / "platform_karpenter.tf").read_text()


def test_ec2nodeclass_imds_hop_limit_allows_pod_creds() -> None:
    """All three EC2NodeClasses MUST set IMDS hop limit 2 (default 1 blocks a pod → node-role creds)."""
    content = (CHARTS / "karpenter" / "templates" / "ec2nodeclass.yaml").read_text()
    assert content.count("httpPutResponseHopLimit: 2") == 3, "cpu, gpu, gpu-p must all set hop limit 2"


def test_node_s3_grant_scoped_to_bucket_not_star() -> None:
    """The node-role S3 grant (S3-direct path) MUST be scoped to the bucket ARN, never `*`."""
    block = _extract_block((ENGINE / "platform_storage.tf").read_text(), "data", "aws_iam_policy_document", "node_s3")
    assert "module.model_store.bucket_arn" in block and '"*"' not in block
    assert "s3:GetObject" in block and "s3:PutObject" in block and "output" in block


def test_onboarder_iam_scopes_workload_ecr_and_bucket() -> None:
    """The onboard job's IAM grants create+push on workload/* and WRITE the shared bucket only (no `*`)."""
    content = (ENGINE / "onboarder.tf").read_text()
    doc = _extract_block(content, "data", "aws_iam_policy_document", "onboarder_extra")
    assert "ecr:CreateRepository" in doc and "workload_repo_arn" in doc
    assert "s3:PutObject" in doc and "module.model_store.bucket_arn" in doc
    assert '"*"' not in doc, "onboard IAM must never use Resource '*'"
    assert "AmazonS3ReadOnlyAccess" in content, "weight-source reads come from the managed policy"


# --- Control-loop placement + HA (system MNG, leader-elected → 2 replicas) ---


def test_control_loop_operators_on_system_ng_and_ha() -> None:
    """Leader-elected operators MUST pin to the tainted system NG AND run 2 replicas (warm standby).

    Placement keeps control-loop pods off Karpenter nodes (where they'd block consolidation);
    2 replicas keep the loop alive across a system-NG node drain. Only proves the .tf SETS the
    keys — that the chart HONORS them (right key spelling) is covered by the live test_platform_placement.
    """
    # (file, release, placement token, replica set-key regex) — set-key None where nested/gated below.
    flat = [
        ("platform_karpenter.tf", "karpenter", "inference/role", r'"replicas"\s*,?\s*value\s*=\s*"2"'),
        (
            "platform_cluster_autoscaler.tf",
            "cluster_autoscaler",
            "inference/role",
            r'"replicaCount"\s*,?\s*value\s*=\s*"2"',
        ),
        ("platform_kro.tf", "kro", "inference/role", r'"deployment.replicaCount"\s*,?\s*value\s*=\s*"2"'),
    ]
    for tf_file, release, placement, replica_re in flat:
        block = _resource((ENGINE / tf_file).read_text(), "helm_release", release)
        assert placement in block, f"{release} must pin to the system NG ({placement})"
        assert re.search(replica_re, block), f"{release} must set 2 replicas"

    # KEDA passes a nested values doc; operator + metrics-apiserver are HA, webhooks (stateless) are not.
    keda = _resource((ENGINE / "platform_keda.tf").read_text(), "helm_release", "keda")
    assert "system_node_selector" in keda and "system_toleration" in keda
    assert re.search(r"operator\s*=\s*\{\s*replicaCount\s*=\s*2", keda)
    assert re.search(r"metricsServer\s*=\s*\{\s*replicaCount\s*=\s*2", keda)

    # Prometheus: memory-limited singleton on the system NG (no HA — StatefulSet).
    prom = _resource((ENGINE / "platform_prometheus.tf").read_text(), "helm_release", "kube_prometheus_stack")
    assert "system_node_selector" in prom and "prometheus_memory_limit" in prom


def test_cluster_autoscaler_discovery_and_scoped_role() -> None:
    """CA discovery tags go on the ASG (MNG tags don't propagate), it balances node groups, and its
    mutating autoscaling actions are tag-scoped to this cluster via Pod Identity (issue #15)."""
    tf = (ENGINE / "platform_cluster_autoscaler.tf").read_text()
    assert 'resource "aws_autoscaling_group_tag"' in tf and "module.node_group.autoscaling_group_name" in tf
    assert "k8s.io/cluster-autoscaler/enabled" in tf
    out = (ENGINE / "modules" / "node_group" / "outputs.tf").read_text()
    assert "autoscaling_group_name" in out and "resources[0].autoscaling_groups[0].name" in out
    ca = _resource(tf, "helm_release", "cluster_autoscaler")
    assert "balance-similar-node-groups" in ca
    assoc = _extract_block(tf, "resource", "aws_eks_pod_identity_association", "cluster_autoscaler")
    assert "module.cluster_autoscaler_role.role_arn" in assoc
    assert "autoscaling:SetDesiredCapacity" in tf and "autoscaling:TerminateInstanceInAutoScalingGroup" in tf
    assert "k8s.io/cluster-autoscaler/" in tf, "mutating ASG actions must be tag-scoped to this cluster"


# --- Cost-safety of the gated high-end GPU pool ---


def test_gpu_p_nodepool_is_cost_safe_isolated() -> None:
    """The gpu-p NodePool MUST be gated + carry a DISTINCT tier taint (key != the label key).

    A pod reaches P only by opting into BOTH the nvidia-p label AND the inference/gpu-tier
    taint; an under-specified GPU pod falls to the cheaper gpu-g pool. A taint key reused as
    a label key breaks Karpenter's scheduling simulation (verified live).
    """
    nodepools = (CHARTS / "karpenter" / "templates" / "nodepools.yaml").read_text()
    assert "if .Values.gpuP.enabled" in nodepools, "gpu-p pool must be gated"
    gpu_p = nodepools[nodepools.index("name: gpu-p") :]
    assert "inference/accelerator: nvidia-p" in gpu_p and "nvidia.com/gpu" in gpu_p
    assert "key: inference/gpu-tier" in gpu_p and 'value: "high"' in gpu_p, "gpu-p needs a DISTINCT tier taint"
    assert "key: inference/accelerator" not in gpu_p, "tier taint key must NOT reuse the label key"
    karpenter = _resource((ENGINE / "platform_karpenter.tf").read_text(), "helm_release", "karpenter_nodepools")
    assert "gpuP.enabled" in karpenter and "var.enable_gpu_p_nodepool" in karpenter


# --- Local-chart file-edit detection ---


def test_local_chart_releases_carry_content_hash() -> None:
    """Every first-party local-chart helm_release MUST inject local.chart_hashes[...] as a `set`.

    The helm provider keys a release on its `set` values + chart version, NOT the chart dir's
    file contents — without the hash, editing a chart file produces no plan diff (verified live).
    """
    main = (ENGINE / "main.tf").read_text()
    assert "chart_hashes" in main
    for tf_file, release, key in (
        ("platform_karpenter.tf", "karpenter_nodepools", "karpenter"),
        ("platform_kro.tf", "kro_starters", "kro"),
        ("platform_prometheus.tf", "metrics", "metrics"),
        ("platform_storage.tf", "storage", "storage"),
    ):
        block = _resource((ENGINE / tf_file).read_text(), "helm_release", release)
        assert f'local.chart_hashes["{key}"]' in block, f"helm_release.{release} must inject its chart hash"
