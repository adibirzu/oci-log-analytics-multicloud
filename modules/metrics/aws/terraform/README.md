# Metrics reader (aws)

Creates an IAM role assumable via web identity with only cloudwatch:GetMetricData, ListMetrics and GetMetricStatistics (plus tag:GetResources when allow_tag_lookup=true). Reuses existing_oidc_provider_arn by default; create_oidc_provider=true creates one.

Read-only, OIDC federation only; no static keys. Style and trust patterns mirror `modules/logs/aws/terraform`.

## Inputs

account_id, name_prefix, existing_oidc_provider_arn | create_oidc_provider, oidc_issuer, oidc_subject, oidc_audience, allow_tag_lookup. No account/subscription/project defaults.

## Outputs

role_arn

## Enable

`mcctl enable aws --signal metrics`
