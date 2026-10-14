# Container Instances support only EMPTYDIR and CONFIGFILE volumes, so vault
# secrets cannot be mounted. Containers receive the secret OCID in
# MC_*_SECRET_OCID and read the bundle with the instance's resource principal.
# The one exception is the APM data key for the OTel gateway, see README/DEPLOY.

locals {
  common_env = {
    OCI_REGION      = var.region
    OCI_AUTH_MODE   = "resource_principal"
    LOGAN_NAMESPACE = local.la_namespace
  }

  cloud_env = {
    aws = {
      AWS_ACCOUNT_ID    = var.aws_account_id
      AWS_REGION        = var.aws_region
      AWS_SQS_QUEUE_URL = var.aws_queue_url
      AWS_SQS_DLQ_URL   = var.aws_dlq_url
      AWS_ROLE_ARN      = var.aws_role_arn
      AWS_S3_BUCKET     = var.aws_s3_bucket
      AWS_S3_PREFIX     = var.aws_s3_prefix
      AWS_AUTH_MODE     = "web_identity"
    }
    azure = {
      AZURE_TENANT_ID          = var.azure_tenant_id
      AZURE_CLIENT_ID          = var.azure_client_id
      AZURE_SUBSCRIPTION_ID    = var.azure_subscription_id
      AZURE_EVENTHUB_NAMESPACE = var.azure_eventhub_namespace
      AZURE_EVENTHUB_NAME      = var.azure_eventhub_name
    }
    gcp = {
      GCP_PROJECT_ID          = var.gcp_project_id
      GCP_ALLOWED_PROJECT_IDS = var.gcp_project_id
      GCP_PUBSUB_SUBSCRIPTION = var.gcp_subscription
    }
  }

  logs_containers = [
    for c in local.logs_clouds : {
      name    = "logs-${c}"
      image   = var.logs_collector_image
      command = ["python", "-m", "collectors.logs", "--source", c]
      args    = []
      env = merge(
        local.common_env,
        { LOGAN_LOG_GROUP_OCID = local.log_group_ocid },
        local.cloud_env[c],
        local.need_vault ? { MC_FEDERATION_SECRET_OCID = oci_vault_secret.federation[c].id } : {},
      )
      volume = ""
    }
  ]

  bridge_containers = local.run_bridge ? [{
    name    = "metrics-bridge"
    image   = var.metrics_bridge_image
    command = []
    args    = []
    env = merge(local.common_env, {
      OCI_COMPARTMENT_ID = var.compartment_ocid
      BRIDGE_LISTEN      = "127.0.0.1:4319"
    })
    volume = ""
  }] : []

  # APM sinks need the APM domain; fall back to Monitoring only when it is off.
  sink = var.enable_apm ? var.metrics_sink : "monitoring"

  puller_receivers = {
    for c in local.enabled_clouds : c => (
      c == "aws" ? "awscloudwatch/${var.aws_region}" : (c == "azure" ? "azuremonitor" : "googlecloudmonitoring")
    )
  }

  puller_config = local.run_puller ? templatefile("${path.module}/templates/metrics-pull.yaml.tpl", {
    aws_enabled   = var.enable_aws
    azure_enabled = var.enable_azure
    gcp_enabled   = var.enable_gcp
    aws_region    = var.aws_region
    sink          = local.sink
    receivers     = local.puller_receivers
    exporters     = join(", ", local.sink == "monitoring" ? ["otlphttp/bridge"] : (local.sink == "apm" ? ["otlphttp/apm"] : ["otlphttp/bridge", "otlphttp/apm"]))
  }) : ""

  puller_uses_apm = local.run_puller && local.sink != "monitoring"

  puller_containers = local.run_puller ? [{
    name    = "otel-metrics-pull"
    image   = var.otel_collector_image
    command = []
    args    = ["--config=/etc/otelcol/metrics-pull.yaml"]
    env = merge(
      { METRICS_BRIDGE_ENDPOINT = "http://127.0.0.1:4319" },
      local.cloud_env["azure"], local.cloud_env["gcp"],
      { AWS_REGION = var.aws_region },
      local.puller_uses_apm ? { OCI_APM_ENDPOINT = local.apm_endpoint, OCI_APM_PRIVATE_DATAKEY = local.apm_private_key } : {},
    )
    volume = "pull-config"
  }] : []

  gateway_config = local.run_gateway ? templatefile("${path.module}/templates/apm-gateway.yaml.tpl", {}) : ""

  gateway_containers = local.run_gateway ? [{
    name    = "otel-apm-gateway"
    image   = var.otel_collector_image
    command = []
    args    = ["--config=/etc/otelcol/apm-gateway.yaml"]
    env = {
      APM_SAMPLING_PERCENT    = tostring(var.apm_sampling_percent)
      OCI_APM_ENDPOINT        = local.apm_endpoint
      OCI_APM_PRIVATE_DATAKEY = local.apm_private_key
    }
    volume = "gateway-config"
  }] : []

  containers = concat(local.logs_containers, local.bridge_containers, local.puller_containers, local.gateway_containers)

  config_volumes = merge(
    local.run_puller ? { "pull-config" = { file = "metrics-pull.yaml", data = local.puller_config } } : {},
    local.run_gateway ? { "gateway-config" = { file = "apm-gateway.yaml", data = local.gateway_config } } : {},
  )
}

resource "oci_container_instances_container_instance" "runtime" {
  count                    = length(local.containers) > 0 ? 1 : 0
  compartment_id           = var.compartment_ocid
  availability_domain      = local.ad_name
  display_name             = "${local.name}-runtime"
  shape                    = var.container_shape
  container_restart_policy = "ALWAYS"

  shape_config {
    ocpus         = var.container_ocpus
    memory_in_gbs = var.container_memory_gbs
  }

  vnics {
    subnet_id             = local.subnet_id
    is_public_ip_assigned = false
  }

  dynamic "containers" {
    for_each = local.containers
    content {
      display_name          = containers.value.name
      image_url             = containers.value.image
      command               = length(containers.value.command) > 0 ? containers.value.command : null
      arguments             = length(containers.value.args) > 0 ? containers.value.args : null
      environment_variables = { for k, v in containers.value.env : k => v if v != "" }

      dynamic "volume_mounts" {
        for_each = containers.value.volume != "" ? [containers.value.volume] : []
        content {
          mount_path  = "/etc/otelcol"
          volume_name = volume_mounts.value
        }
      }
    }
  }

  dynamic "volumes" {
    for_each = local.config_volumes
    content {
      name        = volumes.key
      volume_type = "CONFIGFILE"
      configs {
        file_name = volumes.value.file
        data      = base64encode(volumes.value.data)
      }
    }
  }

  depends_on = [oci_identity_policy.runtime]
}
