# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately. Do not open a public issue.

Use GitHub's private vulnerability reporting: open the repository's **Security** tab and choose
**Report a vulnerability**. Include the affected file or module, reproduction steps and impact.
You will receive an acknowledgement and a remediation timeline.

## Never post secrets or identifiers

When sharing logs or configuration, redact before posting:

- OCIDs, tenancy namespaces, AWS account IDs, Azure subscription/tenant IDs, GCP project IDs
- Public and private IP addresses, domain names of internal systems
- Keys, tokens, APM data keys, wallet passwords, service-account JSON, PEM files
- Personal names and email addresses

Replace real values with `<PLACEHOLDER>` tokens or `${VAR}` references.

## Design stance

- No static cloud keys: the cloud side uses OIDC federation, the OCI side uses resource or instance
  principals. Operator credentials are only accepted behind an explicit opt-in.
- Secrets travel through SDK calls or `0600` temporary files with cleanup, never argv, logs or
  Terraform state.
- Destructive operations require a reviewed plan and an explicit typed confirmation.
- `scripts/redaction_gate.sh` blocks identifiers and secrets from being committed.
