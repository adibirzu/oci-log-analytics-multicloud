output "subscription" {
  value = google_pubsub_subscription.collector.id
}
output "dead_letter_subscription" {
  value = google_pubsub_subscription.dead_letter.id
}
output "topic" {
  value = google_pubsub_topic.logs.id
}
output "workload_identity_provider_resource" {
  description = "Exact provider resource for GCP_WIF_PROVIDER_RESOURCE; no token/key."
  value       = google_iam_workload_identity_pool_provider.collector.name
}
output "collector_member" {
  value = local.collector_member
}
output "sink_filter" {
  value = local.filter
}

output "dead_letter_topic" {
  value = google_pubsub_topic.dead_letter.id
}
