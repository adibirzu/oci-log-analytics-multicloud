output "eventhub_namespace" {
  description = "Exact collector endpoint, without a connection string or SAS key."
  value       = "${azurerm_eventhub_namespace.this.name}.servicebus.windows.net"
}
output "eventhub_name" {
  value = azurerm_eventhub.diagnostics.name
}
output "consumer_group_name" {
  value = azurerm_eventhub_consumer_group.collector.name
}
output "collector_client_id" {
  description = "Client identifier for the WIF app or managed identity; not a secret."
  value       = local.client_id
}
output "tenant_id" {
  value = var.tenant_id
}
output "subscription_id" {
  value = var.subscription_id
}
