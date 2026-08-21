"""HCL structure + wiring assertions for the engine.

Scope (deliberately narrow — mirrors the eks-oidc template test): this guards only
invariants where drift is BOTH silent AND costly — load-bearing depends_on / destroy
ordering, air-gap image sourcing (pull-through vs vendored), security-scoped IAM,
control-loop placement + HA, and cost-safety of the gated GPU pool. It does NOT snapshot
arbitrary resource bodies, docs, or decorative wiring; those change often and a test that
merely mirrors them is churn, not a guard. Parsing is regex + brace-matching (no hcl2).
"""

import re
import subprocess

import yaml

from inference_tf_aws_eks_karpenter.template import TEMPLATE_PATH

ENGINE = TEMPLATE_PATH / "engine"
CHARTS = TEMPLATE_PATH / "charts"


def _render_karpenter(**values: str) -> list[dict]:
    """Render the karpenter chart with helm and return its parsed YAML docs.

    A set-values dict overrides chart values.
    """
    sets = ["--set", "gpuAmiId=a", "--set", "nodeInstanceProfile=p", "--set", "discoveryTag=t"]
    for k, v in values.items():
        sets += ["--set", f"{k}={v}"]
    out = subprocess.run(
        ["helm", "template", str(CHARTS / "karpenter"), *sets], check=True, capture_output=True, text=True
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _nodeclass(docs: list[dict], name: str) -> dict:
    """The EC2NodeClass doc named `name`."""
    for d in docs:
        if d.get("kind") == "EC2NodeClass" and d["metadata"]["name"] == name:
            return d
    raise AssertionError(f"EC2NodeClass {name} not rendered")


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


def _iter_resource_blocks(content: str) -> list[tuple[str, str, str]]:
    """Yield (kind, node_id, body) for every resource/module block in content.

    node_id is the terraform ref: `<type>.<name>` for a resource, `module.<name>` for a
    module. This lets the depends_on graph span both — this template's node group is a
    `module.node_group` call, not a bare aws_eks_node_group resource.
    """
    blocks: list[tuple[str, str, str]] = []
    pattern = re.compile(r'(resource\s+"([\w-]+)"\s+"([\w-]+)"|module\s+"([\w-]+)")\s*\{')
    for m in pattern.finditer(content):
        depth, idx = 1, m.end()
        while idx < len(content) and depth > 0:
            depth += {"{": 1, "}": -1}.get(content[idx], 0)
            idx += 1
        node_id = f"module.{m.group(4)}" if m.group(4) else f"{m.group(2)}.{m.group(3)}"
        blocks.append((m.group(1), node_id, content[m.end() : idx - 1]))
    return blocks


def _depends_on_refs(body: str) -> set[str]:
    """Return the set of terraform refs (`<type>.<name>` and `module.<name>`) in a
    block's depends_on list (empty if none)."""
    match = re.search(r"depends_on\s*=\s*\[(.*?)\]", body, re.DOTALL)
    if not match:
        return set()
    inner = match.group(1)
    refs = {f"module.{n}" for n in re.findall(r"module\.(\w+)", inner)}
    refs |= {f"{t}.{n}" for t, n in re.findall(r"(?<!module\.)\b([a-z][\w-]*)\.(\w+)", inner) if t != "module"}
    return refs


# Resources whose destroy needs the cluster to still be usable — they run against the
# cluster API (K8s provider) or evict pods/finalizers (Helm uninstall). Each MUST keep
# both the admin authorization AND the node groups alive until it is deleted.
_AUTH_AT_DESTROY_TYPES = ("kubernetes_", "helm_release")

# The only access-policy associations that grant a caller (human/CI, i.e. the identity
# Terraform's kubernetes/helm provider authenticates as) cluster API authorization.
_ADMIN_AUTH_NODES = frozenset(
    {
        "aws_eks_access_policy_association.admin_role",
        "aws_eks_access_policy_association.admin_user",
    }
)

# The node group must outlive every K8s/Helm resource on destroy: Helm uninstall evicts
# pods and runs finalizers, which need nodes to schedule on. Here it is a module call.
_NODE_GROUP_NODES = frozenset({"module.node_group"})


def _build_depends_on_graph() -> tuple[dict[str, set[str]], list[str]]:
    """Return (graph, k8s_nodes): the depends_on graph across engine/*.tf and the list
    of kubernetes_*/helm_release resource nodes within it."""
    graph: dict[str, set[str]] = {}
    k8s_nodes: list[str] = []
    for tf_file in sorted(ENGINE.glob("*.tf")):
        for _kind, node_id, body in _iter_resource_blocks(tf_file.read_text()):
            graph[node_id] = _depends_on_refs(body)
            if node_id.startswith(_AUTH_AT_DESTROY_TYPES):
                k8s_nodes.append(node_id)
    return graph, k8s_nodes


def _reaches(graph: dict[str, set[str]], start: str, targets: frozenset[str]) -> bool:
    """True if any target is reachable from start by following depends_on edges."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if cur in targets:
            return True
        stack.extend(graph.get(cur, set()))
    return False


def test_k8s_resources_guard_admin_auth_through_destroy() -> None:
    """Every kubernetes_*/helm_release resource MUST reach an admin access-policy
    association via its depends_on chain, so admin authorization outlives it on destroy.

    The chain may be transitive: e.g. a helm_release depends on null_resource.cluster_addons,
    which depends on the associations directly. This is the invariant that broke in eks-oidc
    when the fluent-bit SA and cluster-autoscaler lacked the guard — a concurrent/interrupted
    destroy tore down the access entry first and the provider lost authorization (issue #333).
    """
    graph, k8s_nodes = _build_depends_on_graph()
    assert k8s_nodes, "no kubernetes_*/helm_release resources found in engine/*.tf"

    unguarded = sorted(n for n in k8s_nodes if not _reaches(graph, n, _ADMIN_AUTH_NODES))
    assert not unguarded, (
        f"resource(s) {unguarded} do not reach an admin access-policy association "
        f"({sorted(_ADMIN_AUTH_NODES)}) via depends_on — the K8s/Helm provider can lose "
        "authorization mid-destroy and `jd down` fails with 'Unauthorized'. Add the "
        "associations to the resource's depends_on (directly or via an already-guarded "
        "resource like null_resource.cluster_addons)."
    )


def test_k8s_resources_guard_node_group_through_destroy() -> None:
    """Every kubernetes_*/helm_release resource MUST reach the node group via its
    depends_on chain, so the nodes outlive it on destroy.

    Helm uninstall evicts pods and runs finalizers, which need a node to schedule on;
    if the node group is torn down first the uninstall hangs and `jd down` stalls
    (see the load-bearing destroy order in CLAUDE.md). The chain may be transitive.
    """
    graph, k8s_nodes = _build_depends_on_graph()
    assert k8s_nodes, "no kubernetes_*/helm_release resources found in engine/*.tf"

    unguarded = sorted(n for n in k8s_nodes if not _reaches(graph, n, _NODE_GROUP_NODES))
    assert not unguarded, (
        f"resource(s) {unguarded} do not reach the node group ({sorted(_NODE_GROUP_NODES)}) "
        "via depends_on — the nodes can be torn down mid-destroy and the Helm uninstall "
        "hangs (pods/finalizers have nowhere to run). Add module.node_group to the "
        "resource's depends_on (directly or via an already-guarded resource)."
    )


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


def test_post_eks_cleanup_preserves_destroy_order() -> None:
    """The cleanup sentinel must remain between EKS and the complete VPC network."""
    main = (ENGINE / "main.tf").read_text()
    eks_cluster = re.search(r'module\s+"eks_cluster".*?\n\}', main, re.DOTALL)
    assert eks_cluster is not None
    assert "depends_on = [null_resource.post_eks_vpc_cleanup]" in eks_cluster.group(0)

    content = (ENGINE / "post_eks_cleanup.tf").read_text()
    cleanup = _resource(content, "null_resource", "post_eks_vpc_cleanup")
    assert "module.vpc.vpc_id" in cleanup
    assert 'join(",", module.vpc.private_subnet_ids)' in cleanup
    assert "when        = destroy" in cleanup or "when = destroy" in cleanup.replace("  ", " ")


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

    # DCGM release (nvcr.io-only) MUST repin to its vendored ECR repo AND run GPU-nodes-only
    # (a tolerate-all DaemonSet crashloops on CPU nodes) with a scraped ServiceMonitor.
    dcgm = _resource((ENGINE / "platform_prometheus.tf").read_text(), "helm_release", "dcgm_exporter")
    assert 'aws_ecr_repository.vendored["dcgm_exporter"]' in dcgm, "DCGM release must pull the vendored ECR image"
    assert "nodeSelector.inference/accelerator" in dcgm and "nvidia.com/gpu" in dcgm, "DCGM must be GPU-nodes-only"
    assert "serviceMonitor.additionalLabels.release" in dcgm, "DCGM ServiceMonitor must carry the release label"

    prom = _resource((ENGINE / "platform_prometheus.tf").read_text(), "helm_release", "kube_prometheus_stack")
    assert "local.quay_registry" in prom and "local.k8s_registry" in prom, "prometheus images must pin pull-through"
    assert 'aws_ecr_repository.vendored["grafana"]' in prom, "Grafana must resolve to the vendored ECR repo"
    code = "\n".join(ln for ln in prom.splitlines() if not ln.lstrip().startswith("#"))
    assert "docker.io" not in code and "ghcr.io" not in code, "no prometheus image may reference docker.io/ghcr.io"
    # admissionWebhooks pull a cert-gen image from ghcr.io via chart default (never a literal
    # in-tf string, so the docker.io/ghcr.io text scan can't catch it) — must be disabled.
    assert "admissionWebhooks" in prom and "enabled = false" in prom, (
        "prometheus admissionWebhooks must be disabled (ghcr.io cert-gen dependency, air-gap)"
    )

    # KEDA release (ghcr.io-only) MUST repin ALL THREE images to vendored ECR — no ghcr fallback.
    keda = _resource((ENGINE / "platform_keda.tf").read_text(), "helm_release", "keda")
    for key in ("keda_operator", "keda_metrics_apiserver", "keda_admission_webhooks"):
        assert f'aws_ecr_repository.vendored["{key}"]' in keda, f"KEDA release must repin {key} to vendored ECR"
    keda_code = "\n".join(ln for ln in keda.splitlines() if not ln.lstrip().startswith("#"))
    assert "ghcr.io" not in keda_code, "no KEDA image may reference ghcr.io"

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
    """The node-role model-store grant MUST be read-only and scoped to the bucket ARN, never `*`."""
    block = _extract_block((ENGINE / "platform_storage.tf").read_text(), "data", "aws_iam_policy_document", "node_s3")
    assert "module.model_store.bucket_arn" in block and '"*"' not in block
    assert "s3:GetObject" in block
    assert "s3:PutObject" not in block and "s3:DeleteObject" not in block, "model store is read-only for nodes"


def test_batch_intake_and_output_are_dedicated_buckets() -> None:
    """Batch intake and output MUST be separate s3_bucket module instances with distinct names."""
    content = (ENGINE / "platform_storage.tf").read_text()
    for name, suffix in (("batch_intake", "-batch-in"), ("batch_output", "-batch-out")):
        match = re.search(rf'module\s+"{name}"\s*\{{.*?\n\}}', content, re.DOTALL)
        assert match is not None, f"module.{name} not found"
        block = match.group(0)
        assert "./modules/s3_bucket" in block
        assert suffix in block and "resource_name_prefix" in block


def test_batch_buckets_expire_current_and_noncurrent_objects() -> None:
    """Each batch bucket MUST configure retention through the shared S3 bucket module."""
    content = (ENGINE / "platform_storage.tf").read_text()
    for bucket in ("batch_intake", "batch_output"):
        match = re.search(rf'module\s+"{bucket}"\s*\{{.*?\n\}}', content, re.DOTALL)
        assert match is not None, f"module.{bucket} not found"
        block = match.group(0)
        assert "lifecycle_rule" in block

    module = ENGINE / "modules" / "s3_bucket"
    module_main = (module / "main.tf").read_text()
    module_variables = (module / "variables.tf").read_text()
    assert 'variable "lifecycle_rule"' in module_variables
    lifecycle = _resource(module_main, "aws_s3_bucket_lifecycle_configuration", "this")
    assert "var.lifecycle_rule" in lifecycle
    assert "aws_s3_bucket_versioning.this" in lifecycle
    assert "aws:SecureTransport" in module_main
    assert re.search(r'values\s+= \["false"\]', module_main)


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


# --- Multi-node: EFA device plugin (image supply + AZ co-location quota) ---


def test_efa_registry_inferred_not_hardcoded() -> None:
    """The EFA image's EKS regional registry MUST be inferred from vpc-cni, never hardcoded.

    The EFA plugin lives only on the EKS-managed regional ECR, whose account is
    region-specific. Instead of a region->account map, we read the already-installed
    vpc-cni (aws-node) DaemonSet image and take its <account>.dkr.ecr.<region> prefix —
    whatever EKS resolved for this region/partition. Guards against a regression back
    to a hardcoded account or lookup map.
    """
    images = (ENGINE / "images.tf").read_text()
    assert 'data "kubernetes_resource" "aws_node"' in images, (
        "EFA registry must be inferred from the vpc-cni (aws-node) DaemonSet"
    )
    assert "eks_ecr_registry = " in images and "split(" in images, (
        "eks_ecr_registry must be the split() prefix of the aws-node image, not a literal"
    )
    assert "602401143452" not in images, "the EKS ECR account must never be hardcoded in images.tf"
    assert "eks_ecr_account_by_region" not in images, "no region->account lookup map (that isn't inference)"
    efa_block = images[images.index("efa_vendored_images") :]
    assert "var.enable_efa ?" in efa_block, "EFA vendoring must be gated on enable_efa"
    assert "eks/aws-efa-k8s-device-plugin" in images, "EFA source repo path must be the EKS convention"


def test_efa_image_vendored_and_release_repinned() -> None:
    """EFA is NOT on public.ecr.aws → it MUST be vendored into our ECR and the release repinned."""
    images = (ENGINE / "images.tf").read_text()
    assert "efa_device_plugin" in images, "efa_device_plugin must be a vendored_images entry"
    block = _resource((ENGINE / "platform_efa.tf").read_text(), "helm_release", "efa_device_plugin")
    assert 'aws_ecr_repository.vendored["efa_device_plugin"]' in block, (
        "EFA release image.repository must resolve to the vendored ECR repo"
    )
    assert "local.vendored_tag" in block, "EFA release image.tag must be the vendored tag"
    assert "null_resource.image_vendor" in block, "EFA release must depend on the vendor job completing"


def test_capacity_caps_feed_both_nodepool_limits_and_kueue_quota() -> None:
    """The *_capacity vars are the SINGLE source of truth: same value → NodePool spec.limits
    AND Kueue nominalQuota, so admission (Kueue) can never exceed provisioning (Karpenter).

    Guards against a standalone manual Kueue quota dial: the quota is DERIVED from the
    capacity caps, not set independently.
    """
    karpenter = (ENGINE / "platform_karpenter.tf").read_text()
    kueue = (ENGINE / "platform_kueue.tf").read_text()
    for cap, chart_key in [
        ("var.gpu_g_capacity", "gpuG.gpuLimit"),
        ("var.gpu_p_capacity", "gpuP.gpuLimit"),
        ("var.cpu_capacity", "cpu.cpuLimit"),
        ("var.memory_capacity", "cpu.memoryLimit"),
    ]:
        assert chart_key in karpenter and cap in karpenter, f"{cap} must set the Karpenter NodePool {chart_key}"
    assert "gpuGQuota" in kueue and "var.gpu_g_capacity" in kueue, "Kueue gpuGQuota must derive from gpu_g_capacity"
    assert "gpuQuota" in kueue and "var.gpu_p_capacity" in kueue, "Kueue gpuQuota must derive from gpu_p_capacity"
    assert "cpuQuota" in kueue and "var.cpu_capacity" in kueue, "Kueue cpuQuota must derive from cpu_capacity"
    assert "memoryQuota" in kueue and "var.memory_capacity" in kueue, (
        "Kueue memoryQuota must derive from memory_capacity"
    )
    variables = (ENGINE / "variables.tf").read_text()
    for dead in ("kueue_gpu_g_quota", "kueue_gpu_quota", "kueue_efa_quota", "kueue_cpu_quota", "kueue_memory_quota"):
        assert f'variable "{dead}"' not in variables, f"the manual quota var {dead} must be removed (derived now)"


def test_kueue_efa_quota_derived_from_gpu_quota() -> None:
    """EFA nominalQuota is NOT a separate dial — it equals the flavor's GPU quota (a pod needs
    a GPU to use EFA and a node carries ≤1 EFA, so GPU is the binding constraint)."""
    cfg = (TEMPLATE_PATH / "charts" / "kueue" / "templates" / "kueue-config.yaml").read_text()
    assert ".Values.efaQuota" not in cfg, "EFA must not use a standalone efaQuota value"
    assert cfg.count("{{ .Values.gpuGQuota | quote }}") == 2, "gpu-g flavor: GPU and EFA quota both from gpuGQuota"
    assert cfg.count("{{ .Values.gpuQuota | quote }}") == 2, "gpu-p flavor: GPU and EFA quota both from gpuQuota"


def test_workload_namespace_decoupled_from_kueue_config_chart() -> None:
    """The inference workload namespace MUST be owned by the engine (ungated), not the
    kueue-config chart — else `helm uninstall kueue-config` cascade-deletes the namespace
    and every running inference workload in it. The chart must not declare a Namespace;
    the engine must own it (platform_workloads.tf) and the release must depend on it."""
    cfg = (TEMPLATE_PATH / "charts" / "kueue" / "templates" / "kueue-config.yaml").read_text()
    assert "kind: Namespace" not in cfg, (
        "kueue-config chart must NOT create the workload namespace (uninstall would delete workloads)"
    )
    workloads_tf = (ENGINE / "platform_workloads.tf").read_text()
    assert 'resource "kubernetes_namespace_v1" "workload"' in workloads_tf, (
        "the workload namespace must be an engine-owned kubernetes_namespace_v1 in platform_workloads.tf"
    )
    ns_block = _resource(workloads_tf, "kubernetes_namespace_v1", "workload")
    assert "count" not in ns_block, "the workload namespace must be ungated (no count = var.enable_kueue)"
    block = _resource((ENGINE / "platform_kueue.tf").read_text(), "helm_release", "kueue_config")
    assert "kubernetes_namespace_v1.workload" in block, (
        "kueue_config release must depend_on kubernetes_namespace_v1.workload so the LocalQueue's namespace exists"
    )


# --- Restored coverage: destroy-ordering edges, IAM scope, air-gap, plan-stability ---


def test_platform_charts_depend_on_cluster_addons_barrier() -> None:
    """Every optional Helm chart MUST depend_on null_resource.cluster_addons.

    cluster_addons is the barrier that keeps the addons + admin access associations alive
    until all charts uninstall; a chart that skips it can uninstall AFTER the providers lose
    authorization on `jd down` → "forbidden" / orphaned resources (the eks-oidc lesson). Also
    pins KEDA's create-time edges (Prometheus ServiceMonitor CRD + vendored-image readiness).
    """
    keda = _resource((ENGINE / "platform_keda.tf").read_text(), "helm_release", "keda")
    for dep in (
        "null_resource.cluster_addons",
        "null_resource.pullthrough_ready",
        "helm_release.kube_prometheus_stack",
        "null_resource.image_vendor",
    ):
        assert dep in keda, f"keda.depends_on missing {dep}"

    kro = _resource((ENGINE / "platform_kro.tf").read_text(), "helm_release", "kro")
    for dep in ("null_resource.cluster_addons", "null_resource.pullthrough_ready"):
        assert dep in kro, f"kro.depends_on missing {dep}"

    storage = _resource((ENGINE / "platform_storage.tf").read_text(), "helm_release", "storage")
    assert "null_resource.cluster_addons" in storage, "storage chart must depend_on cluster_addons"
    assert "aws_eks_addon.s3_csi_driver" in storage, "storage chart must depend_on the S3 CSI driver"


def test_s3_csi_uses_dedicated_pod_identity_role() -> None:
    """Mountpoint-for-S3 auths via a DEDICATED Pod Identity role, not the node role (least-privilege)."""
    storage = (ENGINE / "platform_storage.tf").read_text()
    assert 'module "s3_csi_role"' in storage, "a dedicated s3_csi_role must exist"
    csi_doc = _extract_block(storage, "data", "aws_iam_policy_document", "s3_csi")
    assert '"*"' not in csi_doc, "s3_csi grant must never use Resource '*'"
    assert "s3:GetObject" in csi_doc, "mountpoint role must read objects"
    s3_addon = _resource((ENGINE / "eks_addons.tf").read_text(), "aws_eks_addon", "s3_csi_driver")
    assert "aws-mountpoint-s3-csi-driver" in s3_addon, "must install the Mountpoint-for-S3 CSI driver"
    assert "module.s3_csi_role.role_arn" in s3_addon, "s3 CSI addon must use the dedicated role via Pod Identity"


def test_pullthrough_infra_ensure_script_semantics() -> None:
    """pullthrough.tf MUST create-if-absent / adopt / fail-on-divergence, with NO destroy
    provisioner — the shared account-regional infra outlives any single deployment."""
    block = _resource((ENGINE / "pullthrough.tf").read_text(), "null_resource", "pullthrough_infra")
    assert "create-pull-through-cache-rule" in block, "must create the cache rule when absent"
    assert "create-repository-creation-template" in block, "must create the template when absent"
    assert "describe-pull-through-cache-rules" in block, "must probe existing rule for adopt/diverge"
    assert block.count("exit 1") >= 2, "must FAIL on a divergent pre-existing rule/template"
    assert 'interpreter = ["/bin/bash", "-c"]' in block, "local-exec must use bash"
    assert "when        = destroy" not in block and "when = destroy" not in block, (
        "shared pull-through infra must NOT be torn down on destroy (it outlives the deployment)"
    )


def test_node_access_entry_is_ec2_linux_bound_to_node_role() -> None:
    """The node access entry MUST be type EC2_LINUX bound to the node role (API-auth join mechanism)."""
    block = _resource((ENGINE / "main.tf").read_text(), "aws_eks_access_entry", "node")
    assert 'type          = "EC2_LINUX"' in block or 'type = "EC2_LINUX"' in block.replace("  ", " "), (
        "node access entry must be type EC2_LINUX"
    )
    assert "module.node_role.role_arn" in block, "node access entry must bind the node role"


def test_bootstrap_ami_type_resolved_at_root_not_in_module() -> None:
    """ami_type MUST be resolved at the root and passed in concrete.

    A data source inside the node_group module inherits the module's depends_on → ami_type
    "known after apply" → system node group REPLACED on every re-apply (diagnosed live).
    """
    main = (ENGINE / "main.tf").read_text()
    assert re.search(r'data\s+"aws_ec2_instance_type"\s+"bootstrap"', main), (
        "root must own the instance-type data source (not the node_group module)"
    )
    call = re.search(r"module\s+\"node_group\".*?\n\}", main, re.DOTALL)
    assert call is not None
    assert re.search(r"ami_type\s*=\s*local\.bootstrap_ami_type", call.group(0)), (
        "node_group must be called with the root-resolved local.bootstrap_ami_type"
    )
    assert '"default"' not in call.group(0), "ami_type must be concrete, never 'default'"
    module_main = (ENGINE / "modules" / "node_group" / "main.tf").read_text()
    assert 'data "aws_ec2_instance_type"' not in module_main, (
        "node_group module must NOT contain a data source (depends_on cascade forces replacement)"
    )


def test_node_launch_template_carries_mirror_userdata() -> None:
    """The node_group launch template injects the containerd certs.d mirror userData — the node's
    fallback for un-repinned pulls (pause image, chart-hardcoded refs) on the endpoints-only VPC."""
    content = (ENGINE / "modules" / "node_group" / "main.tf").read_text()
    assert "aws_launch_template" in content and "userdata.sh.tftpl" in content, (
        "node_group must render the mirror userData template in its launch template"
    )
    tftpl = (ENGINE / "modules" / "node_group" / "userdata.sh.tftpl").read_text()
    assert "config_path" in tftpl and "certs.d" in tftpl, "userData must set containerd certs.d config_path"
    assert "node.eks.aws" in tftpl, "userData must be a nodeadm NodeConfig MIME part"


# --- FSx for Lustre (opt-in) ---


def test_fsx_resources_are_gated_on_enable_fsx() -> None:
    """Every resource / module / data source declared in platform_fsx.tf MUST be gated by
    `count = var.enable_fsx ? 1 : 0`.

    FSx is opt-in — a PERSISTENT_2 SSD file system has a non-trivial hourly cost floor,
    and it is single-AZ. A resource that leaks past the gate provisions Lustre on every
    deployment (or fails plan on a fresh account with no SLR).
    """
    content = (ENGINE / "platform_fsx.tf").read_text()
    # Every resource | module | data block in the file must be gated.
    declared = re.findall(
        r'^(?:resource|module|data)\s+"[^"]+"\s+"([^"]+)"\s*\{',
        content,
        re.MULTILINE,
    )
    assert declared, "no resources / modules / data sources declared in platform_fsx.tf"
    for name in declared:
        # Extract the matching block by its declaration line.
        pattern = rf'(?:resource|module|data)\s+"[^"]+"\s+"{re.escape(name)}"\s*\{{'
        block_start = re.search(pattern, content)
        assert block_start is not None
        depth, idx = 1, block_start.end()
        while idx < len(content) and depth > 0:
            depth += {"{": 1, "}": -1}.get(content[idx], 0)
            idx += 1
        block = content[block_start.end() : idx - 1]
        assert re.search(r"count\s*=\s*var\.enable_fsx\s*\?\s*1\s*:\s*0", block), (
            f"platform_fsx.tf resource/module/data '{name}' missing count = var.enable_fsx gate"
        )


def test_fsx_uses_persistent2_ssd_lz4_with_dra() -> None:
    """The FSx file system MUST be PERSISTENT_2 SSD (DRA-capable) with LZ4 compression, and
    MUST have a Data Repository Association pointed at the model-store bucket's models/ prefix
    with auto-export off (S3 is source of truth; workloads never write to /models)."""
    content = (ENGINE / "platform_fsx.tf").read_text()
    fs = _resource(content, "aws_fsx_lustre_file_system", "shared")
    assert 'deployment_type             = "PERSISTENT_2"' in fs or 'deployment_type = "PERSISTENT_2"' in fs.replace(
        "  ", " "
    ), "FSx must be PERSISTENT_2 (DRA-capable)"
    assert 'storage_type                = "SSD"' in fs or 'storage_type = "SSD"' in fs.replace("  ", " ")
    assert 'data_compression_type       = "LZ4"' in fs or 'data_compression_type = "LZ4"' in fs.replace("  ", " ")
    # The FSx service-linked role MUST NOT be TF-managed: it is an account-global singleton;
    # a `resource "aws_iam_service_linked_role" "fsx"` would collide across two coexisting
    # FSx-enabled deployments in one account (second apply fails "role has been taken").
    # FSx auto-creates it on the first CreateFileSystem call — a documented one-time race.
    assert 'resource "aws_iam_service_linked_role"' not in content, (
        "platform_fsx.tf must NOT declare aws_iam_service_linked_role — it's an account-global "
        "singleton that breaks two-deployments-in-one-account coexistence"
    )

    dra = _resource(content, "aws_fsx_data_repository_association", "models")
    assert "module.model_store.bucket_name" in dra, "DRA must point at the model_store bucket"
    assert "local.model_store_models_prefix" in dra, "DRA must point at the shared models/ prefix"
    assert "batch_import_meta_data_on_create = true" in dra, "DRA must index pre-existing objects on create"
    # Auto-export MUST be empty (workloads never write to /models via Lustre); auto-import ON.
    assert re.search(r"auto_export_policy\s*\{\s*events\s*=\s*\[\s*\]", dra), (
        "auto_export_policy events must be empty (S3 is source of truth)"
    )
    assert re.search(r'auto_import_policy\s*\{\s*events\s*=\s*\[.*"NEW".*\]', dra, re.DOTALL), (
        "auto_import_policy must include NEW events"
    )
    # DELETED events MUST NOT be in auto_import: an S3-side delete (lifecycle rule fire,
    # compromised principal, misconfigured bucket policy) would otherwise propagate to
    # Lustre within seconds and evict running workloads' weights with no undo path. Explicit
    # resync via `terraform destroy` on the DRA + reapply is the only sanctioned path.
    auto_import_match = re.search(r"auto_import_policy\s*\{\s*events\s*=\s*\[([^\]]*)\]", dra, re.DOTALL)
    assert auto_import_match is not None, "auto_import_policy events list not found"
    assert '"DELETED"' not in auto_import_match.group(1), (
        "auto_import_policy MUST NOT include DELETED — S3-side deletes must not silently propagate to Lustre"
    )

    # imported_file_chunk_size MUST come from the tunable var, not a hardcoded value.
    # 1024 (the AWS default) caps every S3 object <1 GiB on a single OST — no parallel-
    # read across servers, tail-latency-bound throughput for exactly the tensor-file
    # workload FSx is supposed to accelerate. 16 MiB (our new default) fans across OSTs.
    assert "imported_file_chunk_size         = var.fsx_imported_file_chunk_size_mib" in dra or (
        "var.fsx_imported_file_chunk_size_mib" in dra
    ), "DRA imported_file_chunk_size must be var-driven, not a hardcoded 1024"


def test_fsx_sg_rules_are_sg_referenced_not_cidr() -> None:
    """FSx SG rules MUST source by SG reference (not CIDR).

    CIDR-based rules — including 0.0.0.0/0 — do not satisfy EFA requirements even if they
    allow all traffic on all ports (per the FSx docs), so an EFA-enabled NodePool that
    tries to mount FSx would silently fail. Also verify the four Lustre-protocol ports
    are opened both ways (988 + 1018-1023).
    """
    content = (ENGINE / "platform_fsx.tf").read_text()
    # No CIDR sources anywhere in an SG rule body.
    for kind in ("aws_vpc_security_group_ingress_rule", "aws_vpc_security_group_egress_rule"):
        for name in re.findall(rf'resource\s+"{kind}"\s+"([^"]+)"', content):
            block = _resource(content, kind, name)
            assert "cidr_ipv4" not in block and "cidr_ipv6" not in block, (
                f"{kind}.{name} must source by SG reference, not CIDR (EFA composition)"
            )
            assert "referenced_security_group_id" in block, f"{kind}.{name} missing referenced_security_group_id"

    # 988 + 1018-1023 must appear on both ingress and egress sides.
    for port_kw in ("from_port                    = 988", "from_port                    = 1018"):
        assert content.count(port_kw) >= 2, f"expected multiple SG rules with {port_kw!r} (ingress + egress)"


def test_fsx_csi_role_uses_pod_identity_and_least_privilege() -> None:
    """The FSx CSI controller SA MUST authenticate via Pod Identity to a dedicated role
    scoped to Describe-only actions (static provisioning). Explicitly NOT AmazonFSxFullAccess
    — that managed policy grants Delete/Update, giving a chart-supply-chain compromise
    enough authority to nuke the file system AND hang jd down on state drift.
    """
    content = (ENGINE / "platform_fsx.tf").read_text()
    role = re.search(r'module\s+"fsx_csi_role"\s*\{(.*?)\n\}', content, re.DOTALL)
    assert role is not None
    role_body = role.group(1)
    assert "pod_identity_trust" in role_body, "FSx CSI role must use the shared pod_identity_trust doc"
    # Explicit deny-list: no managed AmazonFSxFullAccess (too broad — includes Delete/Update).
    assert "AmazonFSxFullAccess" not in content, (
        "FSx CSI role must NOT attach AmazonFSxFullAccess (grants Delete/Update on every FS in "
        "the account — least-privilege violation the rest of this repo doesn't tolerate)"
    )

    # A dedicated Describe-only inline policy MUST exist on the role.
    doc = _extract_block(content, "data", "aws_iam_policy_document", "fsx_csi")
    assert "fsx:DescribeFileSystems" in doc, "fsx_csi inline policy must grant DescribeFileSystems"
    # Anything mutating on FSx is explicitly forbidden by this shape.
    for forbidden in ("fsx:DeleteFileSystem", "fsx:UpdateFileSystem", "fsx:CreateFileSystem"):
        assert forbidden not in doc, (
            f"fsx_csi inline policy MUST NOT grant {forbidden} — static provisioning does not need it"
        )
    inline = _resource(content, "aws_iam_role_policy", "fsx_csi")
    assert "module.fsx_csi_role[0].role_name" in inline, "inline policy must attach to the FSx CSI role"

    assoc = _resource(content, "aws_eks_pod_identity_association", "fsx_csi")
    assert 'service_account = "fsx-csi-controller-sa"' in assoc, (
        "Pod Identity association must bind the fsx-csi-controller-sa SA"
    )
    assert "module.fsx_csi_role[0].role_arn" in assoc, "Pod Identity association must use the gated FSx CSI role"


def test_storage_chart_wires_fsx_values_conditionally() -> None:
    """The storage helm_release MUST pass fsx.enabled to the chart (always) and inject the
    file system id / dns name / mount name / capacity only when var.enable_fsx is true.

    The chart's fsx-mount.yaml short-circuits on `fsx.enabled: false`, so unconditional
    injection is safe but wasted API traffic; a var.enable_fsx-gated concat() keeps plan
    diffs quiet when FSx is off.
    """
    block = _resource((ENGINE / "platform_storage.tf").read_text(), "helm_release", "storage")
    assert '"fsx.enabled"' in block, "storage chart must always receive fsx.enabled"
    assert "var.enable_fsx ?" in block, "FSx set values must be conditional on var.enable_fsx"
    for key in ("fsx.fileSystemId", "fsx.dnsName", "fsx.mountName"):
        assert f'"{key}"' in block, f"storage chart missing FSx wiring for {key}"
    assert "aws_fsx_lustre_file_system.shared[0]" in block, "FSx values must reference the gated FSx FS resource"


def test_fsx_pv_template_is_gated_by_values_flag() -> None:
    """The FSx PV/PVC chart template MUST be wrapped in `if .Values.fsx.enabled` so a chart
    render with fsx.enabled=false produces zero PV/PVC objects (the FSx-off default)."""
    tmpl = (CHARTS / "storage" / "templates" / "fsx-mount.yaml").read_text()
    assert "if .Values.fsx.enabled" in tmpl, "fsx-mount.yaml must be gated on .Values.fsx.enabled"
    # The FSx CSI driver requires <fs-id>::<mount-name> for volumeHandle (not the fs-id alone).
    assert "{{ .Values.fsx.fileSystemId }}::{{ .Values.fsx.mountName }}" in tmpl, (
        "volumeHandle MUST be <fs-id>::<mount-name> for the FSx CSI driver (not just the FS id)"
    )
    # flock is the load-bearing mount option for SafeTensors / mmap consumers.
    assert "flock" in tmpl, "FSx PV must mount with flock (POSIX file locks)"


def test_fsx_pv_has_az_node_affinity() -> None:
    """The FSx PV MUST embed a topology.kubernetes.io/zone nodeAffinity pointing at
    the FSx AZ. Without it, workloads that mount the PVC can schedule cross-AZ and
    every read/write silently pays inter-AZ transfer + higher latency.

    Terraform passes fsx.availabilityZone into the chart set-values in
    platform_storage.tf; the PV template consumes it here.
    """
    tmpl = (CHARTS / "storage" / "templates" / "fsx-mount.yaml").read_text()
    assert "nodeAffinity:" in tmpl, "FSx PV must have spec.nodeAffinity (AZ-pin the workload)"
    assert "topology.kubernetes.io/zone" in tmpl, "PV nodeAffinity must be keyed on topology.kubernetes.io/zone"
    assert "{{ .Values.fsx.availabilityZone" in tmpl, (
        "PV nodeAffinity zone value must be templated from .Values.fsx.availabilityZone"
    )
    # And the storage chart wiring must actually pass the AZ through.
    storage = _resource((ENGINE / "platform_storage.tf").read_text(), "helm_release", "storage")
    assert '"fsx.availabilityZone"' in storage, (
        "helm_release.storage must set fsx.availabilityZone (from data.aws_subnet.fsx[0].availability_zone)"
    )


def test_fsx_hydration_job_is_gated_and_scoped() -> None:
    """Hydration Job MUST be gated on both enable_fsx AND non-empty fsx_hydrate_prefixes
    (an FSx-enabled cluster with no prefixes is a valid opt-in shape — don't ship a
    Job that runs for no reason). Job spec MUST AZ-pin to the FSx AZ, tolerate the
    system MNG taint, and mount the platform's model-store-fsx PVC.
    """
    content = (ENGINE / "platform_fsx_hydrate.tf").read_text()

    # Every resource / module / data block MUST be gated on the combined
    # enable_fsx && len(prefixes)>0 sentinel (local.fsx_hydrate_enabled).
    declared = re.findall(
        r'^(?:resource|module|data)\s+"[^"]+"\s+"([^"]+)"\s*\{',
        content,
        re.MULTILINE,
    )
    assert declared, "platform_fsx_hydrate.tf must declare at least one resource"
    for name in declared:
        pattern = rf'(?:resource|module|data)\s+"[^"]+"\s+"{re.escape(name)}"\s*\{{'
        start = re.search(pattern, content)
        assert start is not None
        depth, idx = 1, start.end()
        while idx < len(content) and depth > 0:
            depth += {"{": 1, "}": -1}.get(content[idx], 0)
            idx += 1
        body = content[start.end() : idx - 1]
        # for_each on the prefix map counts as gated (empty map → zero resources).
        gated = re.search(r"count\s*=\s*local\.fsx_hydrate_enabled\s*\?\s*1\s*:\s*0", body) or (
            "for_each = local.fsx_hydrate_prefix_slugs" in body.replace("  ", " ")
        )
        assert gated, (
            f"platform_fsx_hydrate.tf resource/module/data '{name}' missing the "
            f"local.fsx_hydrate_enabled gate OR for_each on fsx_hydrate_prefix_slugs"
        )

    # The Job MUST AZ-pin to the FSx AZ.
    assert "topology.kubernetes.io/zone" in content, "hydration Job must nodeAffinity-pin to the FSx AZ"
    assert "data.aws_subnet.fsx[0].availability_zone" in content, (
        "hydration Job AZ value must come from the same source of truth as the FS "
        "(data.aws_subnet.fsx[0].availability_zone)"
    )
    # And it MUST mount the platform's own model-store-fsx PVC.
    assert 'claim_name = "model-store-fsx"' in content, (
        "hydration Job must mount the platform-owned model-store-fsx PVC (not a per-track one)"
    )
    # ttl + activeDeadline present.
    assert "ttl_seconds_after_finished" in content, "hydration Job must set ttl_seconds_after_finished"
    assert "active_deadline_seconds" in content, "hydration Job must set active_deadline_seconds"


def test_fsx_hydrator_iam_is_scoped_to_our_fs() -> None:
    """The fsx_hydrator IAM policy MUST scope DRT actions to THIS file system's ARN,
    not `Resource: *`. Two coexisting deployments in one account must never be able to
    fire DRTs against each other's file systems via the hydrator SA.
    """
    content = (ENGINE / "platform_fsx_hydrate.tf").read_text()
    doc = _extract_block(content, "data", "aws_iam_policy_document", "fsx_hydrator")
    # Positive: our FS ARN is in the DRT statement's resources.
    assert "aws_fsx_lustre_file_system.shared[0].arn" in doc, (
        "fsx_hydrator DRT statement must resource-scope to our FS ARN"
    )
    # Positive: DRT actions granted.
    for action in ("fsx:CreateDataRepositoryTask", "fsx:DescribeDataRepositoryTasks", "fsx:CancelDataRepositoryTask"):
        assert action in doc, f"fsx_hydrator must grant {action}"
    # Negative: no `Resource: *` on the DRT statement — grep the whole doc for it.
    assert not re.search(r'resources\s*=\s*\[\s*"\*"\s*\]', doc), (
        "fsx_hydrator DRT statement must NOT use Resource: * — scope to our FS ARN"
    )
    # S3 PutObject scoped to the report prefix only (referenced via the local).
    assert "local.fsx_hydrate_report_prefix" in doc, (
        "fsx_hydrator S3 PutObject must scope to the local.fsx_hydrate_report_prefix path"
    )
    # And the local's literal value must be the fsx-drt-reports subpath.
    content_full = (ENGINE / "platform_fsx_hydrate.tf").read_text()
    assert re.search(r'fsx_hydrate_report_prefix\s*=\s*"fsx-drt-reports"', content_full), (
        "local.fsx_hydrate_report_prefix must equal 'fsx-drt-reports'"
    )


def test_fsx_per_unit_throughput_derives_from_gpu_capacity() -> None:
    """The presets ship `fsx_per_unit_storage_throughput = 0` as a sentinel and
    platform_fsx.tf derives the actual value from BOTH the P-pool flag AND total
    GPU capacity (var.gpu_g_capacity + var.gpu_p_capacity). Big P-heavy clusters
    (> 60 GPUs) get bumped to 1000 MB/s/TiB so cold-scale-out doesn't cap.

    Cold-scale-out (K new pods on K new nodes at once) is the real saturation
    risk — Lustre's per-node cache absorbs steady-state reads but not first-touch
    reads. Kueue's nominalQuota mirrors these capacity vars, so this is the exact
    concurrent-reader ceiling.
    """
    fsx = (ENGINE / "platform_fsx.tf").read_text()
    presets = (ENGINE / "presets" / "defaults-all.tfvars").read_text()
    # Preset sentinel is 0.
    assert re.search(r"fsx_per_unit_storage_throughput\s*=\s*0\b", presets), (
        "preset must ship fsx_per_unit_storage_throughput = 0 (sentinel meaning auto-derive)"
    )
    # platform_fsx.tf has the derivation local referencing enable_gpu_p_nodepool
    # AND both GPU capacity variables.
    assert "local.fsx_per_unit_storage_throughput" in fsx, (
        "platform_fsx.tf must reference local.fsx_per_unit_storage_throughput on the FS resource"
    )
    assert "var.enable_gpu_p_nodepool" in fsx, "the derivation local must branch on var.enable_gpu_p_nodepool"
    assert "var.gpu_g_capacity" in fsx and "var.gpu_p_capacity" in fsx, (
        "the derivation local must consider total GPU capacity (gpu_g + gpu_p) — cold-scale-out "
        "of many pods is the real saturation risk, and Kueue nominalQuota mirrors these vars"
    )
    # Assert the three tier thresholds appear in the file (250, 500, 1000).
    for tier in ("250", "500", "1000"):
        assert re.search(rf"\b{tier}\b", fsx), f"derivation must produce tier {tier}"


def test_fsx_platform_info_configmap_is_discoverable() -> None:
    """The fsx-platform-info ConfigMap MUST publish every field a consumer needs
    to consume FSx from a Kubernetes-native surface — replaces the deploy-time
    `jupyter-deploy show --output NAME` + Python substitution dance with a
    declarative K8s object.

    Consumers (KRO blocks, workload initContainers, kubectl scripts) discover
    this by the `platform.inference/kind: storage` label — the peer
    s3-mount-platform-info ConfigMap shares that label.
    """
    cm = _resource((ENGINE / "platform_fsx.tf").read_text(), "kubernetes_config_map_v1", "fsx_platform_info")
    # Gated on enable_fsx (otherwise there's no FSx to describe).
    assert "count = var.enable_fsx" in cm, "fsx-platform-info ConfigMap must be gated on enable_fsx"
    # Namespace = the shared workload namespace (where consumers live).
    assert "kubernetes_namespace_v1.workload.metadata[0].name" in cm, (
        "fsx-platform-info ConfigMap must live in the workload namespace, not kube-system"
    )
    # Grouping label so consumers list by kind, not by name.
    assert '"platform.inference/kind"    = "storage"' in cm.replace("  ", " ").replace("  ", " ") or re.search(
        r'"platform\.inference/kind"\s*=\s*"storage"', cm
    ), "ConfigMap must carry platform.inference/kind = storage for label-based discovery"
    # Every field a KRO block or initContainer needs.
    required_keys = (
        "fileSystemId",
        "dnsName",
        "mountName",
        "availabilityZone",
        "dataRepositoryPath",
        "mountPath",
        "storageCapacityGib",
        "perUnitThroughputMBpsPerTiB",
        "aggregateGBpsMax",
        "platformPvcName",
    )
    for key in required_keys:
        assert f"{key}" in cm, f"fsx-platform-info ConfigMap missing data key '{key}'"


def test_s3_mount_platform_info_configmap_is_discoverable() -> None:
    """The s3-mount-platform-info ConfigMap MUST ship UNCONDITIONALLY (S3-mount is
    always on) as a peer to fsx-platform-info, so tracks can enumerate every
    available storage backend by listing `-l platform.inference/kind=storage` in
    the workload namespace — one API surface across backends, not "check for
    ConfigMap X else hardcode name Y."

    Peer invariants with fsx-platform-info:
      - workload namespace (same as FSx peer)
      - platform.inference/kind = storage label
      - platform.inference/backend = s3-mount label (backend-specific)
      - platformPvcName + platformPvcNamespace so tracks discover the PVC
        location without hardcoding
      - capabilities string so tracks that need RWX/POSIX reject S3-mount on
        read rather than failing at pod-schedule time
    """
    cm = _resource(
        (ENGINE / "platform_storage.tf").read_text(),
        "kubernetes_config_map_v1",
        "s3_mount_platform_info",
    )
    # UNCONDITIONAL — no count expression at all (S3-mount is always installed).
    assert "count =" not in cm, (
        "s3-mount-platform-info ConfigMap must ship unconditionally — S3-mount is always on, unlike FSx which is opt-in"
    )
    # Workload namespace — where consumers live.
    assert "kubernetes_namespace_v1.workload.metadata[0].name" in cm, (
        "s3-mount-platform-info ConfigMap must live in the workload namespace"
    )
    # Discovery label + backend identifier.
    assert re.search(r'"platform\.inference/kind"\s*=\s*"storage"', cm), (
        "ConfigMap must carry platform.inference/kind = storage (peer with fsx-platform-info)"
    )
    assert re.search(r'"platform\.inference/backend"\s*=\s*"s3-mount"', cm), (
        "ConfigMap must carry platform.inference/backend = s3-mount"
    )
    # Every field a track needs to discover + evaluate S3-mount.
    required_keys = (
        "bucketName",
        "region",
        "modelsPrefix",
        "mountPath",
        "dataRepositoryPath",
        "platformPvcName",
        "platformPvcNamespace",
        "capabilities",
    )
    for key in required_keys:
        assert f"{key}" in cm, f"s3-mount-platform-info ConfigMap missing data key '{key}'"
    # Capabilities MUST surface the constraint honestly — tracks that need RWX
    # or full POSIX consult this to reject the backend on read.
    assert "read-only" in cm and "partial-posix" in cm, (
        "capabilities must surface Mountpoint's constraints (read-only + partial-posix) "
        "so tracks reject the backend when they need RWX/full-POSIX"
    )


def test_fsx_observability_alarms_and_grafana_ds() -> None:
    """Observability layer (platform_fsx_observability.tf) MUST ship:
    - a CloudWatch log-metric-filter + alarm on FSx event log WARN/ERROR/FAILED
    - a FreeStorageCapacity alarm keyed on 20% of provisioned capacity
    - read + write throughput-saturation alarms at 70% of FS ceiling (15 min sustained)
    - a Grafana CloudWatch data source with an IAM policy scoped to AWS/FSx
    """
    content = (ENGINE / "platform_fsx_observability.tf").read_text()
    # Four alarms — capacity, event-log, read saturation, write saturation.
    for alarm in ("fsx_events", "fsx_free_capacity", "fsx_read_saturation", "fsx_write_saturation"):
        block = _resource(content, "aws_cloudwatch_metric_alarm", alarm)
        assert "count = var.enable_fsx" in block, f"alarm '{alarm}' must be gated on enable_fsx"
    # Saturation alarms MUST divide by the derived FS ceiling local (not hardcoded)
    # so bumping fsx_per_unit_storage_throughput auto-retunes them.
    for alarm in ("fsx_read_saturation", "fsx_write_saturation"):
        block = _resource(content, "aws_cloudwatch_metric_alarm", alarm)
        assert "local.fsx_throughput_5min_ceiling_bytes" in block, (
            f"'{alarm}' expression must divide by local.fsx_throughput_5min_ceiling_bytes "
            "(so alarm auto-tunes when the throughput tier is bumped)"
        )
        # 15-minute sustained window (3 × 5-min periods) so transient spikes
        # (KEDA cold-start, hydration replay) don't page.
        assert re.search(r"evaluation_periods\s*=\s*3\b", block), (
            f"'{alarm}' must require 15 min sustained (evaluation_periods = 3) — otherwise "
            "cold-scale-out spikes false-positive"
        )
    # Log-metric filter wired.
    lmf = _resource(content, "aws_cloudwatch_log_metric_filter", "fsx_events")
    assert "aws_cloudwatch_log_group.fsx[0].name" in lmf, "log-metric-filter must target the FSx event log group"
    # Grafana CW IAM: action-scoped only. cloudwatch:namespace is NOT a valid
    # condition key for GetMetricData/ListMetrics/GetMetricStatistics per the
    # CloudWatch Service Authorization Reference — only PutMetricData supports
    # it. Namespace-restriction is enforced by the datasource's
    # customMetricsNamespaces config, not IAM.
    doc = _extract_block(content, "data", "aws_iam_policy_document", "grafana_cloudwatch")
    assert "cloudwatch:namespace" not in doc, (
        "Grafana CloudWatch policy must NOT set cloudwatch:namespace condition — it's not "
        "a valid condition key for read actions and every request would silently AccessDenied"
    )
    assert "cloudwatch:GetMetricData" in doc, "policy must grant GetMetricData"
    assert "cloudwatch:ListMetrics" in doc, "policy must grant ListMetrics (Grafana discovery)"
    # Datasource ConfigMap has the grafana_datasource label the chart's sidecar watches.
    cm = _resource(content, "kubernetes_config_map_v1", "grafana_fsx_datasource")
    assert 'grafana_datasource = "1"' in cm, (
        "Grafana datasource ConfigMap must carry the `grafana_datasource: 1` label for auto-discovery"
    )


def test_onboarder_backstop_and_workload_repos_cluster_scoped() -> None:
    """The chart-onboarder MUST digest-vendor + backstop (no non-air-gapped ref escapes), and its
    imperative workload/* ECR repos MUST embed resource_name_prefix (two-deployments-coexist)."""
    script = (ENGINE / "onboarder.py").read_text()
    assert "skopeo" in script and "--all" in script, "workload images must be digest-vendored with --all (multi-arch)"
    assert "BACKSTOP FAILED" in script, "backstop must fail the build when a ref doesn't resolve to our ECR/S3"
    assert "onboard_chart" in script and "onboard_graph" in script, "must support both the chart and graph paths"
    content = (ENGINE / "onboarder.tf").read_text()
    assert 'workload_repo_prefix = "${local.resource_name_prefix}/workload"' in content, (
        "workload repo prefix must be cluster-scoped via resource_name_prefix"
    )
    doc = _extract_block(content, "data", "aws_iam_policy_document", "onboarder_extra")
    assert "ecr:TagResource" in doc, "onboarder must be allowed to tag the repos it creates"


def test_gpu_parallel_pull_gate_only_on_gpu_classes() -> None:
    """gpu_parallel_image_pull toggles the FastImagePull gate on gpu + gpu-p (never cpu)."""
    on = _render_karpenter(**{"gpuParallelPull.enabled": "true"})
    for name in ("gpu", "gpu-p"):
        assert "FastImagePull: true" in _nodeclass(on, name)["spec"].get("userData", ""), (
            f"{name} must carry the FastImagePull gate when enabled"
        )
    assert "FastImagePull" not in _nodeclass(on, "cpu")["spec"].get("userData", ""), (
        "cpu must never carry the FastImagePull gate"
    )
    off = _render_karpenter(**{"gpuParallelPull.enabled": "false"})
    for name in ("gpu", "gpu-p", "cpu"):
        assert "FastImagePull" not in _nodeclass(off, name)["spec"].get("userData", ""), (
            f"{name} must not carry the gate when disabled"
        )


def test_gpu_parallel_pull_ebs_tuning_only_on_gpu_classes() -> None:
    """When enabled, gpu + gpu-p root volumes get the SOCI EBS throughput/IOPS; cpu never does."""
    on = _render_karpenter(**{"gpuParallelPull.enabled": "true"})
    for name in ("gpu", "gpu-p"):
        ebs = _nodeclass(on, name)["spec"]["blockDeviceMappings"][0]["ebs"]
        assert ebs.get("throughput") == 600 and ebs.get("iops") == 3000, f"{name} missing SOCI EBS tuning: {ebs}"
    cpu_ebs = _nodeclass(on, "cpu")["spec"]["blockDeviceMappings"][0]["ebs"]
    assert "throughput" not in cpu_ebs and "iops" not in cpu_ebs, f"cpu must not get SOCI EBS tuning: {cpu_ebs}"
    off = _render_karpenter(**{"gpuParallelPull.enabled": "false"})
    for name in ("gpu", "gpu-p"):
        ebs = _nodeclass(off, name)["spec"]["blockDeviceMappings"][0]["ebs"]
        assert "throughput" not in ebs, f"{name} must not get EBS tuning when disabled: {ebs}"


def test_workload_repo_cleanup_runs_after_creators() -> None:
    """Workload repository creators must be destroyed before the scoped cleanup."""
    onboarder = (ENGINE / "onboarder.tf").read_text()
    cleanup = _resource(onboarder, "null_resource", "workload_repo_cleanup")
    assert "prefix = local.workload_repo_prefix" in cleanup
    assert "when        = destroy" in cleanup or "when = destroy" in cleanup.replace("  ", " ")

    onboarder_module = re.search(r'module\s+"onboarder".*?\n\}', onboarder, re.DOTALL)
    assert onboarder_module is not None
    assert "depends_on = [null_resource.workload_repo_cleanup]" in onboarder_module.group(0)

    image_build = (ENGINE / "platform_image_build.tf").read_text()
    image_build_module = re.search(r'module\s+"image_build".*?\n\}', image_build, re.DOTALL)
    assert image_build_module is not None
    assert "depends_on = [null_resource.workload_repo_cleanup]" in image_build_module.group(0)
