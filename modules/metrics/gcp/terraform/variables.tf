variable "project_id" {
  description = "Exact project whose metrics are read."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid GCP project ID."
  }
}

variable "name_prefix" {
  description = "Prefix for the service account ID."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,13}$", var.name_prefix))
    error_message = "name_prefix must be 2-14 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "workload_identity_pool_id" {
  description = "New dedicated federation pool ID."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,30}[a-z0-9]$", var.workload_identity_pool_id))
    error_message = "workload_identity_pool_id must be 4-32 lowercase letters, digits or hyphens."
  }
}

variable "workload_identity_provider_id" {
  description = "New dedicated OIDC provider ID."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,30}[a-z0-9]$", var.workload_identity_provider_id))
    error_message = "workload_identity_provider_id must be 4-32 lowercase letters, digits or hyphens."
  }
}

variable "oidc_issuer" {
  description = "Exact HTTPS issuer reachable by Google."
  type        = string

  validation {
    condition     = can(regex("^https://[A-Za-z0-9.-]+(/[A-Za-z0-9._/-]+)?$", var.oidc_issuer))
    error_message = "An exact HTTPS OIDC issuer is required."
  }
}

variable "oidc_subject" {
  description = "Exact workload subject; no wildcard trust."
  type        = string

  validation {
    condition     = length(trimspace(var.oidc_subject)) > 0 && length(var.oidc_subject) <= 127 && !strcontains(var.oidc_subject, "*")
    error_message = "Supply an exact subject of 1-127 characters without wildcards."
  }
}

variable "oidc_audience" {
  description = "Exact audience of the projected OIDC JWT."
  type        = string

  validation {
    condition     = length(trimspace(var.oidc_audience)) > 0 && !strcontains(var.oidc_audience, "*")
    error_message = "An exact audience without wildcards is required."
  }
}
