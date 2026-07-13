# === Kueue — admission control + gang scheduling for multi-node inference ===
#
# Kueue gates LWS workloads behind GPU/CPU quota — the entire group is admitted
# atomically or stays suspended. Two features are always-on when enabled:
#
# 1. Prometheus ServiceMonitor: queue health visibility.
# 2. waitForPodsReady: evicts + requeues a workload on partial provisioning failure
#    (the safety net when a gang's leader lands but its workers can't be provisioned).
#
# NOT Topology-Aware Scheduling: TAS derives admissible AZ domains from nodes that
# ALREADY exist, which deadlocks a scale-from-zero GPU pool. AZ co-location for
# multi-node NCCL/EFA is instead enforced at provision time by the LWS exclusive-topology
# annotation on the workload (see charts/kueue/templates/kueue-config.yaml).
#
# Placement: controller on the tainted system NG.
# Images/chart: OCI on registry.k8s.io (pull-through, no vendoring).

locals {
  kueue_namespace = "kueue-system"
}

resource "helm_release" "kueue" {
  count = var.enable_kueue ? 1 : 0

  name             = "kueue"
  repository       = "oci://registry.k8s.io/kueue/charts"
  chart            = "kueue"
  version          = var.kueue_chart_version
  namespace        = local.kueue_namespace
  create_namespace = true

  set = [
    # Repin controller image to pull-through URI.
    { name = "controllerManager.manager.image.repository", value = "${local.ecr_registry}/registry-k8s/kueue/kueue" },
    { name = "controllerManager.manager.image.tag", value = "v${var.kueue_chart_version}" },

    # Two replicas so a leader failover (system-NG node drain) keeps a warm standby;
    # the managerConfig sets leaderElect: true, so only one controller is active.
    { name = "controllerManager.replicas", value = "2" },

    # Prometheus ServiceMonitor (top-level toggle)
    { name = "enablePrometheus", value = "true" },

    # System NG placement (controllerManager.nodeSelector / controllerManager.tolerations)
    { name = "controllerManager.nodeSelector.inference/role", value = "system" },
    { name = "controllerManager.tolerations[0].key", value = "inference/role" },
    { name = "controllerManager.tolerations[0].operator", value = "Equal" },
    { name = "controllerManager.tolerations[0].value", value = "system" },
    { name = "controllerManager.tolerations[0].effect", value = "NoSchedule" },
  ]

  # managerConfig.controllerManagerConfigYaml is an opaque YAML STRING — not
  # deep-merged. We must carry the FULL default (including integrations.frameworks
  # which registers LWS) and append waitForPodsReady. Verified with:
  #   helm template kueue ... | grep -E 'leaderworkerset|waitForPodsReady'
  values = [yamlencode({
    managerConfig = {
      controllerManagerConfigYaml = <<-YAML
        apiVersion: config.kueue.x-k8s.io/v1beta2
        kind: Configuration
        health:
          healthProbeBindAddress: :8081
        metrics:
          bindAddress: :8443
        webhook:
          port: 9443
        leaderElection:
          leaderElect: true
          resourceName: c1f6bfd2.kueue.x-k8s.io
        controller:
          groupKindConcurrency:
            Job.batch: 5
            Pod: 5
            Workload.kueue.x-k8s.io: 5
            LocalQueue.kueue.x-k8s.io: 1
            ClusterQueue.kueue.x-k8s.io: 1
            ResourceFlavor.kueue.x-k8s.io: 1
        clientConnection:
          qps: 50
          burst: 100
        waitForPodsReady:
          timeout: 15m
          recoveryTimeout: 3m
          blockAdmission: true
          requeuingStrategy:
            timestamp: Eviction
            backoffLimitCount: 3
            backoffBaseSeconds: 60
            backoffMaxSeconds: 3600
        integrations:
          frameworks:
            - "batch/job"
            - "kubeflow.org/mpijob"
            - "ray.io/rayjob"
            - "ray.io/rayservice"
            - "ray.io/raycluster"
            - "jobset.x-k8s.io/jobset"
            - "trainer.kubeflow.org/trainjob"
            - "kubeflow.org/paddlejob"
            - "kubeflow.org/pytorchjob"
            - "kubeflow.org/tfjob"
            - "kubeflow.org/xgboostjob"
            - "kubeflow.org/jaxjob"
            - "workload.codeflare.dev/appwrapper"
            - "pod"
            - "deployment"
            - "statefulset"
            - "leaderworkerset.x-k8s.io/leaderworkerset"
      YAML
    }
  })]

  depends_on = [
    null_resource.cluster_addons,
    null_resource.pullthrough_ready,
    module.node_group,
    helm_release.kube_prometheus_stack,
    helm_release.leader_worker_set,
  ]
}

# --- Kueue queue configuration (charts/kueue) ---
#
# First-party local chart: ResourceFlavors, ClusterQueue, LocalQueue. Installed as a
# helm_release (not kubernetes_manifest) because kubernetes_manifest requires a live
# cluster connection at plan time.
resource "helm_release" "kueue_config" {
  count     = var.enable_kueue ? 1 : 0
  name      = "kueue-config"
  chart     = "${path.module}/../charts/kueue"
  namespace = local.kueue_namespace

  set = [
    { name = "clusterQueueName", value = var.kueue_cluster_queue_name },
    { name = "cohortName", value = "gpu-cohort" },
    { name = "gpuQuota", value = tostring(var.kueue_gpu_quota) },
    { name = "gpuLendingLimit", value = tostring(var.kueue_gpu_lending_limit) },
    { name = "cpuQuota", value = tostring(var.kueue_cpu_quota) },
    { name = "memoryQuota", value = var.kueue_memory_quota },
    { name = "workloadNamespace", value = var.workload_namespace },
    # Offer the p-tier flavor only when the P node pool exists (else P workloads admit then
    # hang Pending). P gang scheduling is NOT e2e-tested (scarce H100 capacity) — g is.
    { name = "enableGpuPNodes", value = tostring(var.enable_gpu_p_nodepool) },
    { name = "chartContentHash", value = local.chart_hashes["kueue"] },
  ]

  # The LocalQueue lives in the shared workload namespace, which the engine owns; it must
  # exist first (and on destroy, this release is removed before the namespace is deleted).
  depends_on = [helm_release.kueue, kubernetes_namespace_v1.workload]
}
