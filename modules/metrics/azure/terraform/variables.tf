variable "subscription_id" {
  description = "Exact subscription whose metrics are read; configure the root AzureRM provider to match."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.subscription_id))
    error_message = "subscription_id must be an exact GUID."
  }
}

variable "tenant_id" {
  description = "Exact Entra tenant; configure both root providers to match."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.tenant_id))
    error_message = "tenant_id must be an exact GUID."
  }
}

variable "name_prefix" {
  description = "Stable prefix for new resources."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,35}$", var.name_prefix))
    error_message = "name_prefix must be 2-36 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "resource_group_name" {
  description = "Optional resource group to scope Monitoring Reader to; null scopes to the subscription."
  type        = string
  default     = null

  validation {
    condition     = var.resource_group_name == null || can(regex("^[-\\w._()]{1,90}$", var.resource_group_name))
    error_message = "resource_group_name must be a valid resource group name."
  }
}

variable "identity_type" {
  description = "Create an Entra app registration or a user-assigned managed identity, both federated with no client secret."
  type        = string
  default     = "application"

  validation {
    condition     = contains(["application", "managed_identity"], var.identity_type)
    error_message = "identity_type must be application or managed_identity."
  }
}

variable "identity_resource_group_name" {
  description = "Existing resource group that holds the managed identity (managed_identity only)."
  type        = string
  default     = null
}

variable "location" {
  description = "Azure region for the managed identity (managed_identity only)."
  type        = string
  default     = null
}

variable "oidc_issuer" {
  description = "Exact trusted workload OIDC issuer; must be reachable by Entra."
  type        = string

  validation {
    condition     = can(regex("^https://[^/?#]+(/[^?#]*)?$", var.oidc_issuer)) && !strcontains(var.oidc_issuer, "*")
    error_message = "oidc_issuer must be an exact HTTPS issuer."
  }
}

variable "oidc_subject" {
  description = "Exact projected workload subject; no wildcard trust."
  type        = string

  validation {
    condition     = length(trimspace(var.oidc_subject)) > 0 && !strcontains(var.oidc_subject, "*")
    error_message = "oidc_subject must be an exact nonempty subject."
  }
}

variable "oidc_audience" {
  description = "Audience for Entra token exchange."
  type        = string
  default     = "api://AzureADTokenExchange"

  validation {
    condition     = var.oidc_audience == "api://AzureADTokenExchange"
    error_message = "Use the Entra workload identity token-exchange audience."
  }
}

variable "tags" {
  description = "Caller-supplied tags for the managed identity."
  type        = map(string)
  default     = {}
}
