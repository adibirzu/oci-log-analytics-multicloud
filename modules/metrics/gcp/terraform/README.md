# Metrics reader (gcp)

Creates a service account with roles/monitoring.viewer on the project, plus a workload identity pool and OIDC provider (attribute_condition pinned to the subject); the federated principal gets roles/iam.workloadIdentityUser on it. No service account keys.

Read-only, OIDC federation only; no static keys. Style and trust patterns mirror `modules/logs/gcp/terraform`.

## Inputs

project_id, name_prefix, workload_identity_pool_id, workload_identity_provider_id, oidc_issuer, oidc_subject, oidc_audience. No account/subscription/project defaults.

## Outputs

service_account_email, workload_identity_provider

## Enable

`mcctl enable gcp --signal metrics`
