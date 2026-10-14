variable "account_id" {
  description = "12-digit AWS account ID the active provider must belong to."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be a 12-digit AWS account ID."
  }
}

variable "name_prefix" {
  description = "Stable prefix for resources created by this module."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,37}$", var.name_prefix))
    error_message = "name_prefix must be 1-38 characters, start with a letter, and contain only lowercase letters, digits, and hyphens."
  }
}

variable "existing_oidc_provider_arn" {
  description = "Existing IAM OIDC provider ARN to reuse (default path)."
  type        = string
  default     = null

  validation {
    condition     = var.existing_oidc_provider_arn == null || can(regex("^arn:[^:]+:iam::[0-9]{12}:oidc-provider/.+$", var.existing_oidc_provider_arn))
    error_message = "existing_oidc_provider_arn must be an IAM OIDC provider ARN."
  }
}

variable "create_oidc_provider" {
  description = "Create the OIDC provider instead of reusing an existing one. Leave false when the logs module or another stack owns it."
  type        = bool
  default     = false
}

variable "oidc_issuer" {
  description = "Exact HTTPS OIDC issuer URL; derives the trust condition key."
  type        = string

  validation {
    condition     = can(regex("^https://[A-Za-z0-9.-]+(/[A-Za-z0-9._/-]+)?$", var.oidc_issuer))
    error_message = "oidc_issuer must be an exact HTTPS issuer URL."
  }
}

variable "oidc_subject" {
  description = "Exact OIDC subject allowed to assume the role; no wildcards."
  type        = string

  validation {
    condition     = length(trimspace(var.oidc_subject)) > 0 && !strcontains(var.oidc_subject, "*")
    error_message = "oidc_subject must be exact, non-empty and wildcard-free."
  }
}

variable "oidc_audience" {
  description = "Exact OIDC audience allowed to assume the role."
  type        = string
  default     = "sts.amazonaws.com"

  validation {
    condition     = length(trimspace(var.oidc_audience)) > 0 && !strcontains(var.oidc_audience, "*")
    error_message = "oidc_audience must be exact and wildcard-free."
  }
}

variable "allow_tag_lookup" {
  description = "Also allow tag:GetResources so the collector can resolve resource tags."
  type        = bool
  default     = false
}
