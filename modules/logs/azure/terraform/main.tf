# Root providers own keyless deployment authentication and exact tenant binding.
# Identity guards are evaluated only during a separately approved live plan.
data "azurerm_client_config" "current" {}
data "azuread_client_config" "current" {}

resource "terraform_data" "guards" {
  lifecycle {
    precondition {
      condition     = lower(data.azurerm_client_config.current.subscription_id) == lower(var.subscription_id) && lower(data.azurerm_client_config.current.tenant_id) == lower(var.tenant_id) && lower(data.azuread_client_config.current.tenant_id) == lower(var.tenant_id)
      error_message = "Both root providers must match the exact subscription and Entra tenant."
    }
    precondition {
      condition     = (var.diagnostic_destination != "storage" && length(var.aks_resource_ids) == 0) || var.diagnostic_storage_account_id != null
      error_message = "Storage destination and optional AKS audit exports require a Storage account ID."
    }
  }
}

resource "azurerm_eventhub_namespace" "this" {
  name                 = var.eventhub_namespace_name
  location             = var.location
  resource_group_name  = var.resource_group_name
  sku                  = "Standard"
  capacity             = var.eventhub_capacity
  auto_inflate_enabled = false
  minimum_tls_version  = "1.2"
  # Azure Monitor diagnostics require namespace SAS; the collector uses WIF.
  local_authentication_enabled = true
  tags                         = var.tags
  depends_on                   = [terraform_data.guards]
}

resource "azurerm_eventhub" "diagnostics" {
  name              = var.eventhub_name
  namespace_id      = azurerm_eventhub_namespace.this.id
  partition_count   = var.partition_count
  message_retention = var.event_retention_days
}

resource "azurerm_eventhub_consumer_group" "collector" {
  name                = var.consumer_group_name
  eventhub_name       = azurerm_eventhub.diagnostics.name
  namespace_name      = azurerm_eventhub_namespace.this.name
  resource_group_name = var.resource_group_name
}

# Microsoft requires Manage+Send+Listen for diagnostic-settings streaming.
# This rule is for Azure Monitor only, never supplied to the collector.
resource "azurerm_eventhub_namespace_authorization_rule" "diagnostics" {
  name                = "${var.name_prefix}-diagnostic-export"
  namespace_name      = azurerm_eventhub_namespace.this.name
  resource_group_name = var.resource_group_name
  listen              = true
  send                = true
  manage              = true
}

locals {
  eventhub_rule_id = var.diagnostic_destination == "eventhub" ? azurerm_eventhub_namespace_authorization_rule.diagnostics.id : null
  eventhub_name    = var.diagnostic_destination == "eventhub" ? azurerm_eventhub.diagnostics.name : null
  storage_id       = var.diagnostic_destination == "storage" ? var.diagnostic_storage_account_id : null
  principal_id     = var.collector_identity_type == "application" ? azuread_service_principal.collector[0].object_id : azurerm_user_assigned_identity.collector[0].principal_id
  client_id        = var.collector_identity_type == "application" ? azuread_application.collector[0].client_id : azurerm_user_assigned_identity.collector[0].client_id
}

resource "azurerm_monitor_diagnostic_setting" "activity" {
  name                           = "${var.name_prefix}-activity-export"
  target_resource_id             = "/subscriptions/${var.subscription_id}"
  eventhub_authorization_rule_id = local.eventhub_rule_id
  eventhub_name                  = local.eventhub_name
  storage_account_id             = local.storage_id
  dynamic "enabled_log" {
    for_each = toset(["Administrative", "Security", "ServiceHealth", "Alert", "Recommendation", "Policy", "Autoscale", "ResourceHealth"])
    content {
      category = enabled_log.value
    }
  }
  depends_on = [terraform_data.guards]
}

resource "azurerm_monitor_aad_diagnostic_setting" "entra" {
  name                           = "${var.name_prefix}-entra-export"
  eventhub_authorization_rule_id = local.eventhub_rule_id
  eventhub_name                  = local.eventhub_name
  storage_account_id             = local.storage_id
  enabled_log {
    category = "SignInLogs"
  }
  enabled_log {
    category = "AuditLogs"
  }
  depends_on = [terraform_data.guards]
}

resource "azurerm_monitor_diagnostic_setting" "aks_audit" {
  for_each           = var.aks_resource_ids
  name               = "${var.name_prefix}-aks-audit-export"
  target_resource_id = each.value
  storage_account_id = var.diagnostic_storage_account_id
  enabled_log {
    category = "kube-audit"
  }
  enabled_log {
    category = "kube-audit-admin"
  }
  depends_on = [terraform_data.guards]
}

resource "azuread_application" "collector" {
  count            = var.collector_identity_type == "application" ? 1 : 0
  display_name     = "${var.name_prefix}-collector"
  sign_in_audience = "AzureADMyOrg"
  depends_on       = [terraform_data.guards]
}
resource "azuread_service_principal" "collector" {
  count     = var.collector_identity_type == "application" ? 1 : 0
  client_id = azuread_application.collector[0].client_id
}
resource "azuread_application_federated_identity_credential" "collector" {
  count          = var.collector_identity_type == "application" ? 1 : 0
  application_id = azuread_application.collector[0].id
  display_name   = "${var.name_prefix}-collector-federation"
  audiences      = [var.collector_oidc_audience]
  issuer         = var.collector_oidc_issuer
  subject        = var.collector_oidc_subject
}
resource "azurerm_user_assigned_identity" "collector" {
  count               = var.collector_identity_type == "managed_identity" ? 1 : 0
  name                = "${var.name_prefix}-collector"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
  depends_on          = [terraform_data.guards]
}
resource "azurerm_federated_identity_credential" "collector" {
  count               = var.collector_identity_type == "managed_identity" ? 1 : 0
  name                = "${var.name_prefix}-collector-federation"
  resource_group_name = var.resource_group_name
  parent_id           = azurerm_user_assigned_identity.collector[0].id
  audience            = [var.collector_oidc_audience]
  issuer              = var.collector_oidc_issuer
  subject             = var.collector_oidc_subject
}
resource "azurerm_role_assignment" "receiver" {
  scope                = azurerm_eventhub.diagnostics.id
  role_definition_name = "Azure Event Hubs Data Receiver"
  principal_id         = local.principal_id
  principal_type       = "ServicePrincipal"
}
resource "azurerm_role_assignment" "checkpoint" {
  scope                = var.checkpoint_container_resource_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = local.principal_id
  principal_type       = "ServicePrincipal"
}
