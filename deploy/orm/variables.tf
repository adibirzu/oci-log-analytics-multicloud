# ---- Resource Manager built-ins (never prompted) --------------------------
variable "region" {
  type        = string
  description = "Region the stack runs in (Resource Manager built-in)."
}

variable "tenancy_ocid" {
  type        = string
  description = "Tenancy (Resource Manager built-in)."
}

variable "compartment_ocid" {
  type        = string
  description = "Compartment that receives every resource and scopes every policy."
}

# ---- Signals ---------------------------------------------------------------
variable "enable_logs" {
  type        = bool
  default     = true
  description = "Deploy the log collectors and the Log Analytics log group."
}

variable "enable_metrics" {
  type        = bool
  default     = false
  description = "Deploy the metrics bridge and the metrics-pull collector."
}

variable "enable_apm" {
  type        = bool
  default     = false
  description = "Deploy the APM gateway collector (and an APM domain unless one is reused)."
}

# ---- Clouds (non-secret identifiers only; federation, no keys) -------------
variable "enable_aws" {
  type    = bool
  default = false
}

variable "aws_account_id" {
  type    = string
  default = ""
}

variable "aws_region" {
  type    = string
  default = ""
}

variable "aws_queue_url" {
  type    = string
  default = ""
}

variable "aws_dlq_url" {
  type    = string
  default = ""
}

variable "aws_role_arn" {
  type    = string
  default = ""
}

variable "aws_s3_bucket" {
  type    = string
  default = ""
}

variable "aws_s3_prefix" {
  type    = string
  default = "AWSLogs/"
}

variable "enable_azure" {
  type    = bool
  default = false
}

variable "azure_tenant_id" {
  type    = string
  default = ""
}

variable "azure_client_id" {
  type    = string
  default = ""
}

variable "azure_subscription_id" {
  type    = string
  default = ""
}

variable "azure_eventhub_namespace" {
  type    = string
  default = ""
}

variable "azure_eventhub_name" {
  type    = string
  default = ""
}

variable "enable_gcp" {
  type    = bool
  default = false
}

variable "gcp_project_id" {
  type    = string
  default = ""
}

variable "gcp_subscription" {
  type    = string
  default = ""
}

# ---- Log Analytics ---------------------------------------------------------
variable "log_group_name" {
  type    = string
  default = "multicloud"
}

variable "existing_log_group_ocid" {
  type        = string
  default     = ""
  description = "Reuse this Log Analytics log group instead of creating one."
}

# ---- APM -------------------------------------------------------------------
variable "apm_free_tier" {
  type    = bool
  default = true
}

variable "existing_apm_domain_ocid" {
  type        = string
  default     = ""
  description = "Reuse this APM domain instead of creating one."
}

variable "apm_sampling_percent" {
  type    = number
  default = 10
}

variable "metrics_sink" {
  type    = string
  default = "monitoring"

  validation {
    condition     = contains(["monitoring", "apm", "both"], var.metrics_sink)
    error_message = "metrics_sink must be monitoring, apm or both."
  }
}

# ---- Images ----------------------------------------------------------------
variable "logs_collector_image" {
  type    = string
  default = "__IMAGE_REGISTRY__/oci-la-mc-logs-collector:__IMAGE_TAG__"
}

variable "metrics_bridge_image" {
  type    = string
  default = "__IMAGE_REGISTRY__/oci-la-mc-metrics-bridge:__IMAGE_TAG__"
}

variable "otel_collector_image" {
  type    = string
  default = "ghcr.io/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector-contrib:0.162.0"
}

# ---- Runtime ---------------------------------------------------------------
variable "container_shape" {
  type    = string
  default = "CI.Standard.E4.Flex"
}

variable "container_ocpus" {
  type    = number
  default = 2
}

variable "container_memory_gbs" {
  type    = number
  default = 8
}

variable "availability_domain_index" {
  type        = number
  default     = 0
  description = "Index of the availability domain to run in (change if the shape is out of capacity)."
}

# ---- Network ---------------------------------------------------------------
variable "existing_subnet_ocid" {
  type        = string
  default     = ""
  description = "Reuse this private subnet (needs egress via NAT). Empty creates a minimal VCN."
}

variable "vcn_cidr" {
  type    = string
  default = "10.250.0.0/24"
}

variable "name_prefix" {
  type    = string
  default = "mc"
}
