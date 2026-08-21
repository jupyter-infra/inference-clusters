# === FSx observability — alarms + Grafana view ===
#
# Two independent signals, both gated on enable_fsx:
#
#  1. CloudWatch alarms (native, no cluster surface) — fire regardless of whether
#     Grafana/Prometheus are healthy. Cover: event-log WARN/ERROR/FAILED bursts
#     (DRA import failures, service warnings) and FreeStorageCapacity < 20%
#     (capacity headroom before writes 500-error). SNS-agnostic — alarms exist
#     in-Console; users wire their preferred pager by adding alarm_actions later.
#
#  2. Grafana CloudWatch data source — kube-prometheus-stack's Grafana Deployment
#     gets a Pod Identity role scoped to cloudwatch:GetMetricData/ListMetrics on
#     the AWS/FSx namespace, plus a datasource yaml that points at the region's
#     CloudWatch. FSx metrics (DataReadBytes, DataWriteBytes, MetadataOperations,
#     FreeStorageCapacity, LogicalDiskUsage) show up alongside cluster metrics in
#     the same Grafana instance. No new pod, no yace vendoring, reuses the
#     already-provisioned `monitoring` VPC endpoint.

# --- Alarm 1: log-metric filter on the FSx event log ---
#
# CloudWatch alarms can't fire directly on a log stream — the pattern is: filter
# → metric → alarm. Emits FsxEventCount incremented by 1 for each line matching
# WARN|ERROR|FAILED. Alarm on sum > 0 over 5 min (any hit in the window pages).
#
# Per-deployment isolation is baked into the METRIC NAME (not dimensions).
# CloudWatch Logs' PutMetricFilter rejects `dimensions` on a keyword-match
# pattern ("The specified filter pattern does not support dimensions") because
# dimensions require the filter to extract named fields from log events.
# Embedding deployment_id in the metric name gives the same per-deployment
# alarm isolation without the extraction complexity.
resource "aws_cloudwatch_log_metric_filter" "fsx_events" {
  count = var.enable_fsx ? 1 : 0

  name           = "${local.resource_name_prefix}-fsx-events"
  log_group_name = aws_cloudwatch_log_group.fsx[0].name
  # CloudWatch filter pattern — ORs match any keyword. `?` prefix denotes OR.
  pattern = "?WARN ?ERROR ?FAILED"

  metric_transformation {
    # Deployment_id in the metric name — see block comment above for why.
    name      = "FsxEventCount-${random_id.postfix.hex}"
    namespace = "InferenceCluster/FSx"
    value     = "1"
    unit      = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "fsx_events" {
  count = var.enable_fsx ? 1 : 0

  alarm_name          = "${local.resource_name_prefix}-fsx-events"
  alarm_description   = "FSx for Lustre emitted a WARN/ERROR/FAILED event in the last 5 minutes. Check /aws/fsx/${local.resource_name_prefix} log group for the DRA / service-side detail."
  namespace           = "InferenceCluster/FSx"
  metric_name         = "FsxEventCount-${random_id.postfix.hex}"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  tags = local.combined_tags
}

# --- Alarm 2: FreeStorageCapacity < 20% headroom ---
#
# FSx pushes FreeStorageCapacity (bytes) to CloudWatch every minute. Threshold =
# 20% of provisioned capacity (var.fsx_storage_capacity_gib GiB → bytes → 20%).
# 5-minute average avoids flapping on transient scratch bursts.
resource "aws_cloudwatch_metric_alarm" "fsx_free_capacity" {
  count = var.enable_fsx ? 1 : 0

  alarm_name         = "${local.resource_name_prefix}-fsx-low-capacity"
  alarm_description  = "FSx for Lustre FreeStorageCapacity dropped below 20% of provisioned. At 100%-full the FS returns ENOSPC on writes; workloads writing to /models will 500. Increase fsx_storage_capacity_gib or purge stale files."
  namespace          = "AWS/FSx"
  metric_name        = "FreeStorageCapacity"
  statistic          = "Average"
  period             = 300
  evaluation_periods = 2 # 10 min sustained
  # Threshold: 20% of provisioned bytes (GiB → bytes → 20%).
  threshold           = var.fsx_storage_capacity_gib * 1024 * 1024 * 1024 * 0.2
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FileSystemId = aws_fsx_lustre_file_system.shared[0].id
  }

  tags = local.combined_tags
}

# --- Alarm 3 & 4: throughput-saturation ratio (read + write) ---
#
# Fires when 5-min data throughput exceeds 70% of the FS's own ceiling for 3
# consecutive periods (15 minutes sustained). CloudWatch alarm metric_query
# expression divides DataReadBytes / DataWriteBytes sum by
# local.fsx_throughput_5min_ceiling_bytes (which itself is derived from
# capacity × per_unit_throughput at TF-apply time) to get a saturation ratio.
#
# Why 15 min sustained: transient spikes (KEDA cold-start of a big fleet,
# hydration replay after a maintenance event) are expected and fine. Sustained
# saturation is the "bump the throughput tier" signal — see FSX_TUNING.md for
# the manual bump playbook. The 6-hour cooldown on fsx:UpdateFileSystem means
# reactive auto-bumping is deliberately out of scope; alarms drive operator
# action, not automation.
resource "aws_cloudwatch_metric_alarm" "fsx_read_saturation" {
  count = var.enable_fsx ? 1 : 0

  alarm_name          = "${local.resource_name_prefix}-fsx-read-saturation"
  alarm_description   = "FSx sustained > 70% of read-throughput ceiling for 15 min. Bump fsx_per_unit_storage_throughput to the next tier (250 → 500 → 1000) via terraform apply; the 6h fsx:UpdateFileSystem cooldown then locks the choice for the next six hours."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3 # 3 × 5min = 15 min sustained
  threshold           = 0.7
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "ratio"
    expression  = "IF(reads > 0, reads / ${local.fsx_throughput_5min_ceiling_bytes}, 0)"
    label       = "Read throughput saturation ratio (5-min sum / ceiling)"
    return_data = true
  }
  metric_query {
    id = "reads"
    metric {
      metric_name = "DataReadBytes"
      namespace   = "AWS/FSx"
      period      = 300
      stat        = "Sum"
      dimensions = {
        FileSystemId = aws_fsx_lustre_file_system.shared[0].id
      }
    }
  }

  tags = local.combined_tags
}

resource "aws_cloudwatch_metric_alarm" "fsx_write_saturation" {
  count = var.enable_fsx ? 1 : 0

  alarm_name          = "${local.resource_name_prefix}-fsx-write-saturation"
  alarm_description   = "FSx sustained > 70% of write-throughput ceiling for 15 min. Writes bypass Lustre's client-side page cache (which only helps reads), so sustained write pressure is more likely to hit ceiling than reads. Bump fsx_per_unit_storage_throughput to the next tier via terraform apply; 6h cooldown."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3 # 15 min sustained
  threshold           = 0.7
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "ratio"
    expression  = "IF(writes > 0, writes / ${local.fsx_throughput_5min_ceiling_bytes}, 0)"
    label       = "Write throughput saturation ratio (5-min sum / ceiling)"
    return_data = true
  }
  metric_query {
    id = "writes"
    metric {
      metric_name = "DataWriteBytes"
      namespace   = "AWS/FSx"
      period      = 300
      stat        = "Sum"
      dimensions = {
        FileSystemId = aws_fsx_lustre_file_system.shared[0].id
      }
    }
  }

  tags = local.combined_tags
}

# --- Grafana CloudWatch data source (T2.2) ---
#
# Adds a `cloudwatch:GetMetricData` + `ListMetrics` scoped IAM role for Grafana's
# SA (the kube-prometheus-stack Grafana Deployment). Then configures the chart's
# `additionalDataSources` so Grafana registers a CloudWatch data source pointing
# at the deployment's region. Reuses the already-provisioned `monitoring` VPC
# interface endpoint — no NAT, no public egress needed.
#
# Action-scoped policy: CloudWatch read APIs Grafana needs (GetMetricData +
# discovery). `cloudwatch:namespace` is NOT a valid condition key for these
# read actions per the CloudWatch Service Authorization Reference — only
# PutMetricData supports it. A previous attempt scoped GetMetricData to
# `cloudwatch:namespace = "AWS/FSx"`; the condition would never match, IAM
# would deny every request, and the Grafana panels would silently render
# empty (invisible to CI, only caught by looking at Grafana at runtime).
# Scope is achieved via the datasource's `customMetricsNamespaces` config
# (see kubernetes_config_map_v1.grafana_fsx_datasource below), which the
# Grafana query client uses to restrict its `Namespace` param on each call.
data "aws_iam_policy_document" "grafana_cloudwatch" {
  count = var.enable_fsx ? 1 : 0

  statement {
    sid    = "GrafanaCloudWatchReads"
    effect = "Allow"
    actions = [
      "cloudwatch:GetMetricData",
      "cloudwatch:ListMetrics",
      "cloudwatch:GetMetricStatistics",
      "cloudwatch:DescribeAlarmsForMetric",
      "tag:GetResources",
    ]
    resources = ["*"]
  }
}

module "grafana_cloudwatch_role" {
  count = var.enable_fsx ? 1 : 0

  source             = "./modules/iam_role"
  role_name          = "${local.resource_name_prefix}-grafana-cw"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
  combined_tags      = local.combined_tags
}

resource "aws_iam_role_policy" "grafana_cloudwatch" {
  count  = var.enable_fsx ? 1 : 0
  name   = "${local.resource_name_prefix}-grafana-cw"
  role   = module.grafana_cloudwatch_role[0].role_name
  policy = data.aws_iam_policy_document.grafana_cloudwatch[0].json
}

resource "aws_eks_pod_identity_association" "grafana_cloudwatch" {
  count = var.enable_fsx ? 1 : 0

  cluster_name    = module.eks_cluster.cluster_name
  namespace       = local.monitoring_namespace
  service_account = "kube-prometheus-stack-grafana"
  role_arn        = module.grafana_cloudwatch_role[0].role_arn
  tags            = local.combined_tags

  depends_on = [aws_eks_addon.pod_identity_agent]
}

# Grafana CloudWatch data-source config — mounted into Grafana via a
# `sidecar.datasources` label. The kube-prometheus-stack chart auto-discovers
# ConfigMaps with the label `grafana_datasource: "1"` and imports them.
resource "kubernetes_config_map_v1" "grafana_fsx_datasource" {
  count = var.enable_fsx ? 1 : 0

  metadata {
    name      = "grafana-fsx-datasource"
    namespace = local.monitoring_namespace
    labels = {
      # Match the label kube-prometheus-stack's Grafana sidecar watches for.
      grafana_datasource = "1"
    }
  }

  data = {
    "fsx-datasource.yaml" = yamlencode({
      apiVersion = 1
      datasources = [{
        name      = "CloudWatch (FSx)"
        type      = "cloudwatch"
        access    = "proxy"
        uid       = "cloudwatch-fsx"
        isDefault = false
        jsonData = {
          authType      = "default" # Pod Identity via chained credentials
          defaultRegion = data.aws_region.current.id
          # Restrict to the AWS/FSx namespace via the datasource; belt-and-suspenders
          # over the IAM scope.
          customMetricsNamespaces = "AWS/FSx"
        }
      }]
    })
  }

  depends_on = [
    helm_release.kube_prometheus_stack,
    aws_eks_pod_identity_association.grafana_cloudwatch,
  ]
}
