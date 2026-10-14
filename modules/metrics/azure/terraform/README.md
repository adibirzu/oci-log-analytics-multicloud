# Metrics reader (azure)

Creates a federated Entra app registration (or user-assigned managed identity) with Monitoring Reader on the subscription, or on one resource group. No client secret.

Read-only, OIDC federation only; no static keys. Style and trust patterns mirror `modules/logs/azure/terraform`.

## Inputs

subscription_id, tenant_id, name_prefix, resource_group_name (optional scope), identity_type, identity_resource_group_name/location (managed_identity), oidc_issuer, oidc_subject, oidc_audience, tags. No account/subscription/project defaults.

## Outputs

client_id, tenant_id, subscription_id, role_assignment_scope

## Enable

`mcctl enable azure --signal metrics`
