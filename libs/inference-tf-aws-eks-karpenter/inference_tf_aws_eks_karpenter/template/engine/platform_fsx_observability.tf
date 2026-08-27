# FSx observability — CloudWatch alarms + Grafana CloudWatch data source.

# CloudWatch alarms can't fire directly on a log stream; the pattern is
# filter → metric → alarm. Deployment ID is embedded in the METRIC NAME
# (not dimensions) — PutMetricFilter rejects `dimensions` on a keyword-
# match pattern because dimensions require named field extraction.
resource "aws_cloudwatch_log_metric_filter" "fsx_events" {
  count = var.enable_fsx ? 1 : 0

  name           = "${local.resource_name_prefix}-fsx-events"
  log_group_name = aws_cloudwatch_log_group.fsx[0].name
  pattern        = "?WARN ?ERROR ?FAILED"

  metric_transformation {
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

# FSx for Lustre publishes `FreeDataStorageCapacity` (NOT `FreeStorageCapacity`
# — that's the Windows/ONTAP metric; Lustre never emits it, so an alarm on
# that name sits permanently in OK).
resource "aws_cloudwatch_metric_alarm" "fsx_free_capacity" {
  count = var.enable_fsx ? 1 : 0

  alarm_name          = "${local.resource_name_prefix}-fsx-low-capacity"
  alarm_description   = "FSx for Lustre FreeDataStorageCapacity dropped below 20% of provisioned. At 100%-full the FS returns ENOSPC on writes; workloads writing to /models will 500. Increase fsx_storage_capacity_gib or purge stale files."
  namespace           = "AWS/FSx"
  metric_name         = "FreeDataStorageCapacity"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = var.fsx_storage_capacity_gib * 1024 * 1024 * 1024 * 0.2
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FileSystemId = aws_fsx_lustre_file_system.shared[0].id
  }

  tags = local.combined_tags
}

# 15-min sustained > 70% of the FS's own throughput ceiling. Transient spikes
# (KEDA cold-start, hydration replay) are expected; sustained saturation is
# the "bump the throughput tier" signal.
resource "aws_cloudwatch_metric_alarm" "fsx_read_saturation" {
  count = var.enable_fsx ? 1 : 0

  alarm_name          = "${local.resource_name_prefix}-fsx-read-saturation"
  alarm_description   = "FSx sustained > 70% of read-throughput ceiling for 15 min. Bump fsx_per_unit_storage_throughput to the next tier (250 → 500 → 1000) via terraform apply; the 6h fsx:UpdateFileSystem cooldown then locks the choice for the next six hours."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
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
  evaluation_periods  = 3
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

# Grafana CloudWatch data source. `cloudwatch:namespace` is NOT a valid
# condition key for these read actions per the CloudWatch Service
# Authorization Reference — namespace scoping happens via the datasource's
# customMetricsNamespaces config below, not IAM.
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

# kube-prometheus-stack's Grafana sidecar auto-discovers ConfigMaps labeled
# `grafana_datasource: "1"`.
resource "kubernetes_config_map_v1" "grafana_fsx_datasource" {
  count = var.enable_fsx ? 1 : 0

  metadata {
    name      = "grafana-fsx-datasource"
    namespace = local.monitoring_namespace
    labels = {
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
          authType                = "default"
          defaultRegion           = data.aws_region.current.id
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
