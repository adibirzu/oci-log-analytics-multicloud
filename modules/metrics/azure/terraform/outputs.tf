output "client_id" {
  description = "Client ID of the federated metrics reader identity."
  value       = local.client_id
}

output "tenant_id" {
  description = "Entra tenant ID of the metrics reader identity."
  value       = var.tenant_id
}

output "subscription_id" {
  description = "Subscription the Monitoring Reader role is scoped to."
  value       = var.subscription_id
}

output "role_assignment_scope" {
  description = "Scope of the Monitoring Reader assignment."
  value       = local.scope
}
