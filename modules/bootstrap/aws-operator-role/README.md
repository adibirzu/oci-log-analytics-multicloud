# AWS operator role

Least-privilege IAM role that a human operator assumes to run `mcctl enable`,
`mcctl status` and `mcctl disable` for the AWS logs and metrics signals. It
replaces a broad `AdministratorAccess` role.

## Apply once

1. With your current admin credentials, apply this module once:

   ```
   terraform init
   terraform apply \
     -var account_id=<AWS_ACCOUNT_ID> \
     -var trusted_principal_arn=<EXISTING_USER_OR_ROLE_ARN>
   ```

2. Add a profile to `~/.aws/config` using the `role_arn` output:

   ```
   [profile mc-operator]
   source_profile = <YOUR_EXISTING_PROFILE>
   role_arn       = <ROLE_ARN_OUTPUT>
   ```

3. Run `mcctl` with `AWS_PROFILE=mc-operator`, then remove the broad admin role.

## Inputs

| Name | Default | Notes |
| --- | --- | --- |
| `account_id` | none | 12-digit account the provider must belong to |
| `partition` | `aws` | |
| `role_name` | `mc-operator` | must start with `name_prefix` |
| `name_prefix` | `mc-` | all write actions are scoped to names starting with it |
| `trusted_principal_arn` | none | the only principal in the trust policy |
| `require_mfa` | `false` | adds `aws:MultiFactorAuthPresent` |
| `max_session_duration` | `3600` | seconds |
| `oidc_provider_hosts` | `["*"]` | narrow to your issuer host; OIDC provider ARNs are not prefix-named |

Outputs: `role_arn`, `policy_json`.

## Scope and limits

- Writes are limited by ARN to buckets, queues, trails and roles starting with
  `name_prefix`. The module `name_prefix` values (for example `mc`) produce names
  such as `mc-cloudtrail`, which match the default `mc-`.
- Explicit denies: IAM writes outside the prefix, managed-policy attachment,
  permissions-boundary changes, policy creation, and edits to the operator role itself.
- Residual risk: the operator can write inline policies on prefixed roles it
  creates. Enforce a permissions boundary with an SCP if that matters to you.
- CloudWatch read, `sts:GetCallerIdentity` and list calls require `Resource: "*"`.
