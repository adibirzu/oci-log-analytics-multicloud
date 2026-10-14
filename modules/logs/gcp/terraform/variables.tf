variable "project_id" {
  description = "Exact transport project ID containing Pub/Sub and federation resources."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "An exact GCP project ID is required."
  }
}
variable "sink_scope" {
  description = "Scope of the log sink: project or organization."
  type        = string
  default     = "project"
  validation {
    condition     = contains(["project", "organization"], var.sink_scope)
    error_message = "sink_scope must be project or organization."
  }
}
variable "sink_project_id" {
  description = "Exact source project ID; required only for project scope."
  type        = string
  default     = null
  validation {
    condition     = var.sink_project_id == null || can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.sink_project_id))
    error_message = "sink_project_id must be an exact project ID."
  }
}
variable "organization_id" {
  description = "Exact numeric organization ID; required only for organization scope."
  type        = string
  default     = null
  validation {
    condition     = var.organization_id == null || can(regex("^[0-9]+$", var.organization_id))
    error_message = "organization_id must be numeric."
  }
}
variable "sink_name" {
  description = "Owner-selected new log sink name."
  type        = string
  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$", var.sink_name))
    error_message = "sink_name must be 1-100 letters, digits, underscores, periods or hyphens."
  }
}
variable "topic_name" {
  description = "Owner-selected new logs topic name."
  type        = string
}
variable "subscription_name" {
  description = "Owner-selected new collector pull subscription name."
  type        = string
}
variable "dead_letter_topic_name" {
  description = "Owner-selected new sanitized poison topic name."
  type        = string
}
variable "dead_letter_subscription_name" {
  description = "Owner-selected new sanitized poison recovery subscription name."
  type        = string
}
variable "include_data_access" {
  description = "Route existing Data Access logs; does not enable their generation."
  type        = bool
  default     = false
}
variable "include_vpc_flow" {
  description = "Route existing VPC Flow Logs; does not change network logging."
  type        = bool
  default     = false
}
variable "include_gke_audit" {
  description = "Route existing k8s.io Admin Activity and Data Access audit logs."
  type        = bool
  default     = false
}
variable "workload_identity_pool_id" {
  description = "New dedicated federation pool ID."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{3,31}$", var.workload_identity_pool_id)) && !startswith(var.workload_identity_pool_id, "gcp-")
    error_message = "Use a 4-32 character pool ID, not starting with gcp-."
  }
}
variable "workload_identity_provider_id" {
  description = "New dedicated OIDC federation provider ID."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{3,31}$", var.workload_identity_provider_id)) && !startswith(var.workload_identity_provider_id, "gcp-")
    error_message = "Use a 4-32 character provider ID, not starting with gcp-."
  }
}
variable "collector_oidc_issuer" {
  description = "Exact HTTPS issuer reachable by Google; owner-provided projected workload OIDC trust."
  type        = string
  validation {
    condition     = can(regex("^https://[A-Za-z0-9.-]+(/[A-Za-z0-9._/-]+)?$", var.collector_oidc_issuer))
    error_message = "An exact HTTPS OIDC issuer is required."
  }
}
variable "collector_oidc_subject" {
  description = "Exact collector workload subject; no wildcard trust."
  type        = string
  validation {
    condition     = length(trimspace(var.collector_oidc_subject)) > 0 && length(var.collector_oidc_subject) <= 127 && !strcontains(var.collector_oidc_subject, "*")
    error_message = "Supply an exact subject of 1-127 characters without wildcards."
  }
}
variable "collector_oidc_audience" {
  description = "Exact audience of the projected OIDC JWT, accepted by this provider."
  type        = string
  validation {
    condition     = length(trimspace(var.collector_oidc_audience)) > 0 && !strcontains(var.collector_oidc_audience, "*")
    error_message = "An exact audience without wildcards is required."
  }
}
variable "ack_deadline_seconds" {
  description = "Initial subscription ack deadline; runtime renews the in-flight lease."
  type        = number
  default     = 300
  validation {
    condition     = var.ack_deadline_seconds >= 30 && var.ack_deadline_seconds <= 600 && floor(var.ack_deadline_seconds) == var.ack_deadline_seconds
    error_message = "Ack deadline must be an integer from 30 to 600."
  }
}
variable "retention_duration" {
  description = "Logs topic and subscription retention, seconds with s suffix."
  type        = string
  default     = "604800s"
  validation {
    condition     = can(regex("^[0-9]+s$", var.retention_duration)) && try(tonumber(trimsuffix(var.retention_duration, "s")) >= 600 && tonumber(trimsuffix(var.retention_duration, "s")) <= 2678400, false)
    error_message = "Retention must be 600-2678400 seconds."
  }
}
variable "dlq_retention_duration" {
  description = "Sanitized dead-letter topic and recovery subscription retention."
  type        = string
  default     = "1209600s"
  validation {
    condition     = can(regex("^[0-9]+s$", var.dlq_retention_duration)) && try(tonumber(trimsuffix(var.dlq_retention_duration, "s")) >= 600 && tonumber(trimsuffix(var.dlq_retention_duration, "s")) <= 2678400, false)
    error_message = "Dead-letter retention must be 600-2678400 seconds."
  }
}
variable "labels" {
  description = "Owner-supplied labels on transport resources."
  type        = map(string)
  default     = {}
}

resource "terraform_data" "guards" {
  lifecycle {
    precondition {
      condition     = var.sink_scope == "organization" ? var.organization_id != null && var.sink_project_id == null : var.sink_project_id != null && var.organization_id == null
      error_message = "Bind exactly the chosen source scope: project ID or organization ID."
    }
    precondition {
      condition     = alltrue([for name in [var.topic_name, var.subscription_name, var.dead_letter_topic_name, var.dead_letter_subscription_name] : can(regex("^[A-Za-z][A-Za-z0-9._~+%-]{2,254}$", name)) && !startswith(lower(name), "goog")]) && var.topic_name != var.dead_letter_topic_name && var.subscription_name != var.dead_letter_subscription_name
      error_message = "Use valid, distinct Pub/Sub names of 3-255 characters, not starting with goog."
    }
  }
}
