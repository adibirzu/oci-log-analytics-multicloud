output "service_account_email" {
  description = "Metrics reader service account (impersonated, keyless)."
  value       = google_service_account.reader.email
}

output "workload_identity_provider" {
  description = "Full provider resource name for the collector's external-account config."
  value       = google_iam_workload_identity_pool_provider.reader.name
}
