variable "region" {
  description = "AWS region configured on the caller's provider."
  type        = string

  validation {
    condition     = can(regex("^[a-z]{2}(?:-gov)?-[a-z]+-[0-9]$", var.region))
    error_message = "region must be an AWS region name such as eu-west-1 or us-gov-west-1."
  }
}

variable "partition" {
  description = "AWS partition for ARN construction."
  type        = string
  default     = "aws"

  validation {
    condition     = contains(["aws", "aws-us-gov", "aws-cn"], var.partition)
    error_message = "partition must be aws, aws-us-gov, or aws-cn."
  }
}

variable "name_prefix" {
  description = "Stable prefix used for all resources created by this module."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,37}$", var.name_prefix))
    error_message = "name_prefix must be 1-38 characters, start with a letter, and contain only lowercase letters, digits, and hyphens."
  }
}

variable "account_id" {
  description = "12-digit AWS account ID used to scope CloudTrail and S3 policies."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be a 12-digit AWS account ID."
  }
}

variable "bucket_name" {
  description = "Optional globally unique CloudTrail bucket name. Defaults to name_prefix-account_id-cloudtrail."
  type        = string
  default     = null

  validation {
    condition     = var.bucket_name == null || can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.bucket_name))
    error_message = "bucket_name must be a valid S3 bucket name when supplied."
  }
}

variable "trail_name" {
  description = "CloudTrail trail name."
  type        = string
  default     = null

  validation {
    condition     = var.trail_name == null || can(regex("^[a-zA-Z0-9._-]{3,128}$", var.trail_name))
    error_message = "trail_name must be 3-128 characters using letters, digits, periods, underscores, or hyphens."
  }
}

variable "trail_prefix" {
  description = "Object prefix under which CloudTrail writes gzip files and the collector reads them."
  type        = string
  default     = "cloudtrail"

  validation {
    condition     = (can(regex("^[a-zA-Z0-9]$", var.trail_prefix)) || can(regex("^[a-zA-Z0-9][a-zA-Z0-9!_./-]{0,198}[a-zA-Z0-9]$", var.trail_prefix))) && !strcontains(var.trail_prefix, "//")
    error_message = "trail_prefix must be a non-empty object prefix without a leading/trailing slash or repeated slash."
  }
}

variable "data_resources" {
  description = "Optional CloudTrail data-event resources. Management events are always enabled."
  type = list(object({
    type   = string
    values = list(string)
  }))
  default = []

  validation {
    condition = alltrue([
      for resource in var.data_resources : contains([
        "AWS::S3::Object",
        "AWS::Lambda::Function",
        "AWS::DynamoDB::Table",
      ], resource.type) && length(resource.values) > 0 && alltrue([for value in resource.values : length(trimspace(value)) > 0])
    ])
    error_message = "data_resources types must be AWS::S3::Object, AWS::Lambda::Function, or AWS::DynamoDB::Table with non-empty values."
  }
}

variable "lifecycle_expiration_days" {
  description = "Days after which CloudTrail objects expire."
  type        = number
  default     = 90

  validation {
    condition     = var.lifecycle_expiration_days >= 1 && var.lifecycle_expiration_days <= 3650 && floor(var.lifecycle_expiration_days) == var.lifecycle_expiration_days
    error_message = "lifecycle_expiration_days must be an integer from 1 through 3650."
  }
}

variable "multipart_abort_days" {
  description = "Days after which incomplete multipart uploads are aborted."
  type        = number
  default     = 7

  validation {
    condition     = var.multipart_abort_days >= 1 && var.multipart_abort_days <= 365 && floor(var.multipart_abort_days) == var.multipart_abort_days
    error_message = "multipart_abort_days must be an integer from 1 through 365."
  }
}

variable "queue_visibility_timeout_seconds" {
  description = "Collector queue visibility timeout; one message is leased per poll."
  type        = number
  default     = 300

  validation {
    condition     = var.queue_visibility_timeout_seconds >= 30 && var.queue_visibility_timeout_seconds <= 43200 && floor(var.queue_visibility_timeout_seconds) == var.queue_visibility_timeout_seconds
    error_message = "queue_visibility_timeout_seconds must be an integer from 30 through 43200."
  }
}

variable "queue_retention_seconds" {
  description = "Retention for the collector queue; defaults to four days."
  type        = number
  default     = 345600

  validation {
    condition     = var.queue_retention_seconds >= 60 && var.queue_retention_seconds <= 1209600 && floor(var.queue_retention_seconds) == var.queue_retention_seconds
    error_message = "queue_retention_seconds must be an integer from 60 through 1209600."
  }
}

variable "dlq_retention_seconds" {
  description = "Retention for the dead-letter queue; defaults to fourteen days."
  type        = number
  default     = 1209600

  validation {
    condition     = var.dlq_retention_seconds >= 60 && var.dlq_retention_seconds <= 1209600 && floor(var.dlq_retention_seconds) == var.dlq_retention_seconds
    error_message = "dlq_retention_seconds must be an integer from 60 through 1209600."
  }
}

variable "queue_max_receive_count" {
  description = "Collector poison receive threshold; map this output to INGEST_MAX_RECEIVE_COUNT. Native redrive is disabled to preserve sanitized DLQ bodies."
  type        = number
  default     = 5

  validation {
    condition     = var.queue_max_receive_count >= 1 && var.queue_max_receive_count <= 1000 && floor(var.queue_max_receive_count) == var.queue_max_receive_count
    error_message = "queue_max_receive_count must be an integer from 1 through 1000."
  }
}

variable "collector_oidc_provider_arn" {
  description = "Existing IAM OIDC provider ARN for the collector workload identity trust mode."
  type        = string
  default     = null

  validation {
    condition     = var.collector_oidc_provider_arn == null || can(regex("^arn:[^:]+:iam::[0-9]{12}:oidc-provider/.+$", var.collector_oidc_provider_arn))
    error_message = "collector_oidc_provider_arn must be an IAM OIDC provider ARN."
  }
}

variable "collector_oidc_issuer" {
  description = "Exact OIDC issuer URL used to derive the provider condition key."
  type        = string
  default     = null

  validation {
    condition     = var.collector_oidc_issuer == null || can(regex("^https://[A-Za-z0-9.-]+(/[A-Za-z0-9._/-]+)?$", var.collector_oidc_issuer))
    error_message = "collector_oidc_issuer must be an exact HTTPS issuer URL."
  }
}

variable "collector_oidc_subject" {
  description = "Exact OIDC subject allowed to assume the collector role."
  type        = string
  default     = null

  validation {
    condition     = var.collector_oidc_subject == null ? true : length(trimspace(var.collector_oidc_subject)) > 0
    error_message = "collector_oidc_subject must be non-empty when supplied."
  }
}

variable "collector_oidc_audience" {
  description = "Exact OIDC audience allowed to assume the collector role."
  type        = string
  default     = "sts.amazonaws.com"

  validation {
    condition     = length(trimspace(var.collector_oidc_audience)) > 0
    error_message = "collector_oidc_audience must be non-empty."
  }
}

variable "collector_principal_arn" {
  description = "Exact AWS principal ARN for the collector trust mode."
  type        = string
  default     = null

  validation {
    condition     = var.collector_principal_arn == null || can(regex("^arn:[^:]+:iam::[0-9]{12}:role/.+$", var.collector_principal_arn))
    error_message = "collector_principal_arn must be an exact IAM role ARN, including any role path."
  }
}

variable "collector_external_id" {
  description = "Required external ID for the explicit AWS principal trust mode."
  type        = string
  default     = null
  sensitive   = true
}
