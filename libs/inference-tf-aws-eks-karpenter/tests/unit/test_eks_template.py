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
        assert re.search(r"expiration_days\s+= 90", block)
        assert re.search(r"noncurrent_version_expiration_days\s+= 90", block)
        assert re.search(r"abort_incomplete_multipart_upload_days\s+= 7", block)

    module = ENGINE / "modules" / "s3_bucket"
    module_main = (module / "main.tf").read_text()
    module_variables = (module / "variables.tf").read_text()
    assert 'variable "lifecycle_rule"' in module_variables
    lifecycle = _resource(module_main, "aws_s3_bucket_lifecycle_configuration", "this")
    assert "var.lifecycle_rule" in lifecycle
    assert "aws_s3_bucket_versioning.this" in lifecycle
    assert 'resource "aws_s3_bucket_ownership_controls" "this"' in module_main
    assert "BucketOwnerEnforced" in module_main
    assert "aws:SecureTransport" in module_main
    assert re.search(r'values\s+= \["false"\]', module_main)


def test_batch_s3_uses_a_least_privilege_pod_identity_role() -> None:
    """The batch role MUST read intake and read/write output without mutation access."""
    content = (ENGINE / "platform_storage.tf").read_text()
    block = _extract_block(content, "data", "aws_iam_policy_document", "batch_s3")
    assert '"*"' not in block
    assert "module.model_store.bucket_arn" not in block, "batch grant must not touch the model store"
    assert block.count("s3:GetObject") == 2
    assert block.count("s3:PutObject") == 1
    assert "s3:AbortMultipartUpload" not in block
    assert "s3:DeleteObject" not in block and "s3:DeleteBucket" not in block
    for bucket in ("module.batch_intake.bucket_arn", "module.batch_output.bucket_arn"):
        assert bucket in block
    assert 'module "batch_inference_role"' in content
    association = _resource(content, "aws_eks_pod_identity_association", "batch_inference")
    assert "module.batch_inference_role.role_arn" in association
    assert "kubernetes_service_account_v1.batch_inference" in association


def test_batch_storage_contract_is_available_to_workloads() -> None:
    """The template MUST expose fixed Pod Identity and bucket configuration resources."""
    content = (ENGINE / "platform_storage.tf").read_text()
    service_account = _resource(content, "kubernetes_service_account_v1", "batch_inference")
    config_map = _resource(content, "kubernetes_config_map_v1", "batch_storage")
    assert 'batch_inference_service_account_name = "batch-inference"' in content
    assert 'batch_storage_config_map_name        = "batch-storage"' in content
    assert "kubernetes_namespace_v1.workload" in service_account
    assert "AWS_REGION" in config_map
    assert "BATCH_INTAKE_BUCKET" in config_map and "module.batch_intake.bucket_name" in config_map
    assert "BATCH_OUTPUT_BUCKET" in config_map and "module.batch_output.bucket_name" in config_map

    outputs = (ENGINE / "outputs.tf").read_text()
    for name in (
        "batch_intake_bucket",
        "batch_intake_bucket_arn",
        "batch_output_bucket",
        "batch_output_bucket_arn",
        "batch_inference_service_account_name",
        "batch_storage_config_map_name",
        "aws_cli_image_uri",
        "workload_namespace",
    ):
        assert f'output "{name}"' in outputs

    images = (ENGINE / "images.tf").read_text()
    assert 'aws_cli_source_image = "public.ecr.aws/aws-cli/aws-cli:2.27.49"' in images
    assert "local.aws_cli_source_image" in images

    agent = (TEMPLATE_PATH / "AGENT.md.template").read_text()
    assert "serviceAccountName: batch-inference" in agent
    assert "name: batch-storage" in agent
    assert "BATCH_INTAKE_BUCKET" in agent and "BATCH_OUTPUT_BUCKET" in agent


def test_onboarder_iam_scopes_workload_ecr_and_bucket() -> None:
    """The onboard job's IAM grants create+push on workload/* and WRITE the shared bucket only (no `*`)."""
    content = (ENGINE / "onboarder.tf").read_text()
    doc = _extract_block(content, "data", "aws_iam_policy_document", "onboarder_extra")
    assert "ecr:CreateRepository" in doc and "workload_repo_arn" in doc
    assert "s3:PutObject" in doc and "module.model_store.bucket_arn" in doc
    assert '"*"' not in doc, "onboard IAM must never use Resource '*'"
    assert "AmazonS3ReadOnlyAccess" in content, "weight-source reads come from the managed policy"


def test_onboarder_does_not_depend_on_batch_storage() -> None:
    """The onboard job MUST not receive batch bucket names or permissions."""
    content = (ENGINE / "onboarder.tf").read_text()
    assert "module.batch_intake" not in content
    assert "module.batch_output" not in content
    assert "BATCH_INTAKE_S3_URI" not in content
    assert "BATCH_OUTPUT_S3_URI" not in content


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


def test_onboarder_installs_hugging_face_client() -> None:
    content = (ENGINE / "onboarder.tf").read_text()

    assert "huggingface_hub==1.24.0" in content
