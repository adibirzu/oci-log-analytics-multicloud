locals {
  member = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.reader.name}/attribute.reader/${sha256(var.oidc_subject)}"
}

resource "google_service_account" "reader" {
  project      = var.project_id
  account_id   = "${var.name_prefix}-metrics-reader"
  display_name = "${var.name_prefix} metrics reader"
}

resource "google_project_iam_member" "monitoring_viewer" {
  project = var.project_id
  role    = "roles/monitoring.viewer"
  member  = "serviceAccount:${google_service_account.reader.email}"
}

resource "google_iam_workload_identity_pool" "reader" {
  project                   = var.project_id
  workload_identity_pool_id = var.workload_identity_pool_id
  display_name              = var.workload_identity_pool_id
}

resource "google_iam_workload_identity_pool_provider" "reader" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.reader.workload_identity_pool_id
  workload_identity_pool_provider_id = var.workload_identity_provider_id
  attribute_mapping = {
    "google.subject"   = "assertion.sub"
    "attribute.reader" = "'${sha256(var.oidc_subject)}'"
  }
  attribute_condition = "assertion.sub == ${jsonencode(var.oidc_subject)}"
  oidc {
    issuer_uri        = var.oidc_issuer
    allowed_audiences = [var.oidc_audience]
  }
}

# Federated principal may impersonate the reader service account; no keys are created.
resource "google_service_account_iam_member" "impersonate" {
  service_account_id = google_service_account.reader.name
  role               = "roles/iam.workloadIdentityUser"
  member             = local.member
}
