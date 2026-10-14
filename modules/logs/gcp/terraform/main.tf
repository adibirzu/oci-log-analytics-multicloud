# Provider authentication belongs to the keyless, owner-controlled root module.
# No service enablement, credential/key creation, or plan-time discovery here.
locals {
  gke_filter = "protoPayload.serviceName=\"k8s.io\""
  audit_filter = join(" OR ", concat(
    ["log_id(\"cloudaudit.googleapis.com/activity\")"],
    var.include_data_access ? ["log_id(\"cloudaudit.googleapis.com/data_access\")"] : []
  ))
  filter = join(" OR ", concat(
    ["((${local.audit_filter}) AND NOT ${local.gke_filter})"],
    var.include_vpc_flow ? ["(log_id(\"compute.googleapis.com/vpc_flows\") OR log_id(\"networkmanagement.googleapis.com/vpc_flows\"))"] : [],
    var.include_gke_audit ? ["((log_id(\"cloudaudit.googleapis.com/activity\") OR log_id(\"cloudaudit.googleapis.com/data_access\")) AND ${local.gke_filter})"] : []
  ))
  writer_identity = var.sink_scope == "organization" ? google_logging_organization_sink.logs[0].writer_identity : google_logging_project_sink.logs[0].writer_identity
  # Hash mapping avoids unsafe subject characters in the IAM principal URI.
  collector_member = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.collector.name}/attribute.collector/${sha256(var.collector_oidc_subject)}"
}

resource "google_pubsub_topic" "logs" {
  project                    = var.project_id
  name                       = var.topic_name
  message_retention_duration = var.retention_duration
  labels                     = var.labels
  depends_on                 = [terraform_data.guards]
}
resource "google_pubsub_topic" "dead_letter" {
  project                    = var.project_id
  depends_on                 = [terraform_data.guards]
  name                       = var.dead_letter_topic_name
  message_retention_duration = var.dlq_retention_duration
  labels                     = var.labels
}
resource "google_pubsub_subscription" "collector" {
  project                    = var.project_id
  name                       = var.subscription_name
  topic                      = google_pubsub_topic.logs.id
  ack_deadline_seconds       = var.ack_deadline_seconds
  message_retention_duration = var.retention_duration
  labels                     = var.labels
  expiration_policy {
    ttl = ""
  }
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
}
# Native redrive stays disabled: only the collector publishes sanitized poison.
# Retain sanitized envelopes for separate, owner-authorized recovery.
resource "google_pubsub_subscription" "dead_letter" {
  project                    = var.project_id
  name                       = var.dead_letter_subscription_name
  topic                      = google_pubsub_topic.dead_letter.id
  message_retention_duration = var.dlq_retention_duration
  labels                     = var.labels
  expiration_policy {
    ttl = ""
  }
}

resource "google_logging_project_sink" "logs" {
  count                  = var.sink_scope == "project" ? 1 : 0
  project                = var.sink_project_id
  name                   = var.sink_name
  destination            = "pubsub.googleapis.com/${google_pubsub_topic.logs.id}"
  filter                 = local.filter
  unique_writer_identity = true
}
resource "google_logging_organization_sink" "logs" {
  count            = var.sink_scope == "organization" ? 1 : 0
  org_id           = var.organization_id
  name             = var.sink_name
  destination      = "pubsub.googleapis.com/${google_pubsub_topic.logs.id}"
  filter           = local.filter
  include_children = true
  # The Google schema has no unique_writer_identity option for org sinks.
  # Reject the legacy shared identity before its topic publisher grant.
  lifecycle {
    postcondition {
      condition     = startswith(self.writer_identity, "serviceAccount:") && self.writer_identity != "serviceAccount:cloud-logs@system.gserviceaccount.com"
      error_message = "Organization sink must return a dedicated writer identity."
    }
  }
}
resource "google_pubsub_topic_iam_member" "logging_writer" {
  project = var.project_id
  topic   = google_pubsub_topic.logs.name
  role    = "roles/pubsub.publisher"
  member  = local.writer_identity
}
resource "google_iam_workload_identity_pool" "collector" {
  project                   = var.project_id
  workload_identity_pool_id = var.workload_identity_pool_id
  display_name              = var.workload_identity_pool_id
  depends_on                = [terraform_data.guards]
}
resource "google_iam_workload_identity_pool_provider" "collector" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.collector.workload_identity_pool_id
  workload_identity_pool_provider_id = var.workload_identity_provider_id
  attribute_mapping = {
    "google.subject"      = "assertion.sub"
    "attribute.collector" = "'${sha256(var.collector_oidc_subject)}'"
  }
  attribute_condition = "assertion.sub == ${jsonencode(var.collector_oidc_subject)}"
  oidc {
    issuer_uri        = var.collector_oidc_issuer
    allowed_audiences = [var.collector_oidc_audience]
  }
}
# Direct federation; collector gets no project-wide grant or service-account key.
resource "google_pubsub_subscription_iam_member" "collector" {
  project      = var.project_id
  subscription = google_pubsub_subscription.collector.name
  role         = "roles/pubsub.subscriber"
  member       = local.collector_member
}

resource "google_pubsub_topic_iam_member" "collector_dlq" {
  project = var.project_id
  topic   = google_pubsub_topic.dead_letter.name
  role    = "roles/pubsub.publisher"
  member  = local.collector_member
}
