variable "subscription_id" {
  description = "Exact subscription for Activity export; configure the root AzureRM provider to match."
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
variable "location" {
  description = "Owner-selected Azure region for newly created resources."
  type        = string
}
variable "resource_group_name" {
  description = "Existing resource group owned by the deploying root."
  type        = string
}
variable "name_prefix" {
  description = "Stable prefix for new resources; existing resources are not adopted or renamed."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,35}$", var.name_prefix))
    error_message = "name_prefix must be 2-36 lowercase letters, digits or hyphens, starting with a letter."
  }
}
variable "eventhub_namespace_name" {
  description = "Globally unique namespace name. Required; no tenant default."
  type        = string
}
variable "eventhub_name" {
  description = "Exact hub name for Activity and Entra records."
  type        = string
}
variable "consumer_group_name" {
  description = "Dedicated consumer group name for this collector."
  type        = string
}
variable "eventhub_capacity" {
  description = "Standard namespace throughput units; auto-inflate is disabled."
  type        = number
  default     = 1
  validation {
    condition     = var.eventhub_capacity >= 1 && var.eventhub_capacity <= 20 && floor(var.eventhub_capacity) == var.eventhub_capacity
    error_message = "eventhub_capacity must be an integer from 1 to 20."
  }
}
variable "partition_count" {
  description = "Hub partitions; review before creating because decreases are unsupported."
  type        = number
  default     = 2
  validation {
    condition     = var.partition_count >= 1 && var.partition_count <= 32 && floor(var.partition_count) == var.partition_count
    error_message = "partition_count must be an integer from 1 to 32."
  }
}
variable "event_retention_days" {
  description = "Event Hub retention; retries and replay must complete within this window."
  type        = number
  default     = 7
  validation {
    condition     = var.event_retention_days >= 1 && var.event_retention_days <= 7 && floor(var.event_retention_days) == var.event_retention_days
    error_message = "event_retention_days must be an integer from 1 to 7 for Standard."
  }
}
variable "diagnostic_destination" {
  description = "eventhub (preferred collector path) or storage (archive export only)."
  type        = string
  default     = "eventhub"
  validation {
    condition     = contains(["eventhub", "storage"], var.diagnostic_destination)
    error_message = "diagnostic_destination must be eventhub or storage."
  }
}
variable "diagnostic_storage_account_id" {
  description = "Existing owner-controlled Storage account ID, required for storage exports or optional AKS audit archive."
  type        = string
  default     = null
  validation {
    condition     = var.diagnostic_storage_account_id == null ? true : can(regex("^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.Storage/storageAccounts/[^/]+$", var.diagnostic_storage_account_id))
    error_message = "diagnostic_storage_account_id must be an exact Storage account resource ID."
  }
}
variable "collector_identity_type" {
  description = "Create an Entra app or user-assigned managed identity, both with federation and no client secret."
  type        = string
  default     = "application"
  validation {
    condition     = contains(["application", "managed_identity"], var.collector_identity_type)
    error_message = "collector_identity_type must be application or managed_identity."
  }
}
variable "collector_oidc_issuer" {
  description = "Exact trusted workload OIDC issuer; must be reachable by Entra."
  type        = string
  validation {
    condition     = can(regex("^https://[^/?#]+(/[^?#]*)?$", var.collector_oidc_issuer)) && !strcontains(var.collector_oidc_issuer, "*")
    error_message = "collector_oidc_issuer must be an exact HTTPS issuer."
  }
}
variable "collector_oidc_subject" {
  description = "Exact projected workload subject; no wildcard trust."
  type        = string
  validation {
    condition     = length(trimspace(var.collector_oidc_subject)) > 0 && !strcontains(var.collector_oidc_subject, "*")
    error_message = "collector_oidc_subject must be an exact nonempty subject."
  }
}
variable "collector_oidc_audience" {
  description = "Audience for Entra token exchange."
  type        = string
  default     = "api://AzureADTokenExchange"
  validation {
    condition     = var.collector_oidc_audience == "api://AzureADTokenExchange"
    error_message = "Use the Entra workload identity token-exchange audience."
  }
}
variable "checkpoint_container_resource_id" {
  description = "Existing durable checkpoint container ARM ID; collector gets Blob Data Contributor only on this container."
  type        = string
  validation {
    condition     = can(regex("^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.Storage/storageAccounts/[^/]+/blobServices/default/containers/[^/]+$", var.checkpoint_container_resource_id))
    error_message = "checkpoint_container_resource_id must identify an exact blob container."
  }
}
variable "aks_resource_ids" {
  description = "Optional exact AKS IDs to archive kube-audit and kube-audit-admin to diagnostic_storage_account_id. No AKS ingestion parser is claimed."
  type        = set(string)
  default     = []
  validation {
    condition     = alltrue([for id in var.aks_resource_ids : can(regex("^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.ContainerService/managedClusters/[^/]+$", id))])
    error_message = "aks_resource_ids must contain exact AKS resource IDs."
  }
}
variable "tags" {
  description = "Owner-supplied Azure resource tags."
  type        = map(string)
  default     = {}
}
