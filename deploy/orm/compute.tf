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
      AZURE_TENANT_ID               = var.azure_tenant_id
      AZURE_CLIENT_ID               = var.azure_client_id
      AZURE_SUBSCRIPTION_ID         = var.azure_subscription_id
      AZURE_EVENTHUB_NAMESPACE      = var.azure_eventhub_namespace
      AZURE_EVENTHUB_NAME           = var.azure_eventhub_name
      AZURE_EVENTHUB_CONSUMER_GROUP  = var.azure_consumer_group
      AZURE_CHECKPOINT_ACCOUNT_URL  = var.azure_checkpoint_account_url
      AZURE_CHECKPOINT_CONTAINER    = var.azure_checkpoint_container
      AZURE_DLQ_DIRECTORY           = var.azure_dlq_directory
    }
    gcp = {
      GCP_PROJECT_ID            = var.gcp_project_id
      GCP_WIF_PROVIDER_RESOURCE = var.gcp_wif_provider_resource
      GCP_PUBSUB_DLQ_TOPIC      = var.gcp_dlq_topic
      GCP_DLQ_DIRECTORY         = var.gcp_dlq_directory
      GCP_ALLOWED_PROJECT_IDS   = jsonencode([var.gcp_project_id])
      GCP_PUBSUB_SUBSCRIPTION   = var.gcp_subscription
    }
  }

  logs_containers = {
    for c in local.logs_clouds : c => {
      name    = "${local.name}-logs-${c}"
      image   = var.logs_collector_image
      command = ["python", "-m", "collectors.logs", "--source", c]
      env = merge(
        local.common_env,
        { LOGAN_LOG_GROUP_OCID = local.log_group_ocid },
        local.cloud_env[c],
        local.need_vault ? { MC_FEDERATION_SECRET_OCID = oci_vault_secret.federation[c].id } : {},
      )
    }
  }

  bridge_containers = local.run_bridge ? [{
    name    = "metrics-bridge"
    image   = var.metrics_bridge_image
    command = []
    args    = []
    env = merge(local.common_env, {
      OCI_COMPARTMENT_ID = var.compartment_ocid
      BRIDGE_LISTEN      = "0.0.0.0:4319"
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

  # Each puller runs in its own Container Instance because an OCI resource
  # principal applies to every container in that instance.  The bridge is a
  # separate instance; pullers receive only their own federation secret.
  metrics_pullers = local.run_puller ? {
    for c in local.enabled_clouds : c => {
      name = "${local.name}-metrics-${c}"
      config = templatefile("${path.module}/templates/metrics-pull.yaml.tpl", {
        aws_enabled   = c == "aws"
        azure_enabled = c == "azure"
        gcp_enabled   = c == "gcp"
        aws_region    = var.aws_region
        sink          = local.sink
        receivers     = { (c) = local.puller_receivers[c] }
        exporters     = join(", ", local.sink == "monitoring" ? ["otlp_http/bridge"] : (local.sink == "apm" ? ["otlp_http/apm"] : ["otlp_http/bridge", "otlp_http/apm"]))
      })
      env = merge(
        local.common_env,
        local.cloud_env[c],
        {
          METRICS_BRIDGE_ENDPOINT   = "http://${oci_container_instances_container_instance.runtime[0].vnics[0].private_ip}:4319"
          MC_FEDERATION_SECRET_OCID = oci_vault_secret.federation[c].id
        },
        c == "gcp" ? { GCP_METRICS_SERVICE_ACCOUNT_EMAIL = var.gcp_metrics_service_account_email } : {},
        local.sink != "monitoring" ? { OCI_APM_ENDPOINT = local.apm_endpoint, OCI_APM_PRIVATE_DATAKEY = local.apm_private_key } : {},
      )
    }
  } : {}

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

  containers = concat(local.bridge_containers, local.gateway_containers)

  config_volumes = merge(
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

  # IAM references this instance ID to exclude unrelated workloads. The
  # collector restarts on startup refusal while the policy propagates.
}


resource "oci_container_instances_container_instance" "puller_runtime" {
  for_each                 = local.metrics_pullers
  compartment_id           = var.compartment_ocid
  availability_domain      = local.ad_name
  display_name             = each.value.name
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

  containers {
    display_name          = each.value.name
    image_url             = var.otel_federation_image
    arguments             = ["--cloud", each.key, "--config", "/etc/otelcol/metrics-pull.yaml"]
    environment_variables = { for k, v in each.value.env : k => v if v != "" }

    volume_mounts {
      mount_path  = "/etc/otelcol"
      volume_name = "pull-config"
    }
  }

  volumes {
    name        = "pull-config"
    volume_type = "CONFIGFILE"
    configs {
      file_name = "metrics-pull.yaml"
      data      = base64encode(each.value.config)
    }
  }
}

# Keep each cloud assertion in a separate OCI identity boundary. Container
# Instances grant one principal to every container in the instance.
resource "oci_container_instances_container_instance" "log_runtime" {
  for_each                 = local.logs_containers
  compartment_id           = var.compartment_ocid
  availability_domain      = local.ad_name
  display_name             = each.value.name
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

  containers {
    display_name          = each.value.name
    image_url             = each.value.image
    command               = each.value.command
    environment_variables = { for k, v in each.value.env : k => v if v != "" }
  }
}
