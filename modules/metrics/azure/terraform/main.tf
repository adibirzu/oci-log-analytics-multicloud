locals {
  use_app = var.identity_type == "application"
  scope   = var.resource_group_name == null ? "/subscriptions/${var.subscription_id}" : "/subscriptions/${var.subscription_id}/resourceGroups/${var.resource_group_name}"

  principal_id = local.use_app ? azuread_service_principal.reader[0].object_id : azurerm_user_assigned_identity.reader[0].principal_id
  client_id    = local.use_app ? azuread_application.reader[0].client_id : azurerm_user_assigned_identity.reader[0].client_id
}

resource "azuread_application" "reader" {
  count            = local.use_app ? 1 : 0
  display_name     = "${var.name_prefix}-metrics-reader"
  sign_in_audience = "AzureADMyOrg"
}

resource "azuread_service_principal" "reader" {
  count     = local.use_app ? 1 : 0
  client_id = azuread_application.reader[0].client_id
}

resource "azuread_application_federated_identity_credential" "reader" {
  count          = local.use_app ? 1 : 0
  application_id = azuread_application.reader[0].id
  display_name   = "${var.name_prefix}-metrics-federation"
  audiences      = [var.oidc_audience]
  issuer         = var.oidc_issuer
  subject        = var.oidc_subject
}

resource "azurerm_user_assigned_identity" "reader" {
  count               = local.use_app ? 0 : 1
  name                = "${var.name_prefix}-metrics-reader"
  location            = var.location
  resource_group_name = var.identity_resource_group_name
  tags                = var.tags
}

resource "azurerm_federated_identity_credential" "reader" {
  count               = local.use_app ? 0 : 1
  name                = "${var.name_prefix}-metrics-federation"
  resource_group_name = var.identity_resource_group_name
  parent_id           = azurerm_user_assigned_identity.reader[0].id
  audience            = [var.oidc_audience]
  issuer              = var.oidc_issuer
  subject             = var.oidc_subject
}

resource "azurerm_role_assignment" "monitoring_reader" {
  scope                = local.scope
  role_definition_name = "Monitoring Reader"
  principal_id         = local.principal_id
  principal_type       = "ServicePrincipal"

  lifecycle {
    precondition {
      condition     = local.use_app || (var.identity_resource_group_name != null && var.location != null)
      error_message = "managed_identity requires identity_resource_group_name and location."
    }
  }
}
