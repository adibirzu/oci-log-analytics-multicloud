# Deploy the OCI side

The Resource Manager stack in `deploy/orm/` creates everything the OCI side needs in any tenancy, region and compartment. It uses the Resource Manager identity (no API keys) and the built-in `region`, `tenancy_ocid` and `compartment_ocid`.

## One click

[![Deploy to Oracle Cloud](https://docs.oracle.com/en-us/iaas/Content/Resources/Images/deploy-to-oracle-cloud.svg)](https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=https://github.com/adibirzu/oci-log-analytics-multicloud/releases/latest/download/oci-log-analytics-multicloud-orm.zip)

The release workflow replaces `https://github.com/adibirzu/oci-log-analytics-multicloud` with the repository URL and attaches the zip to each release.

## Manual upload

```bash
IMAGE_REGISTRY=ghcr.io/<owner> scripts/build_orm_zip.sh   # writes dist/oci-log-analytics-multicloud-orm.zip
```

Console: Developer Services, Resource Manager, Stacks, Create stack, My configuration, upload the zip. `IMAGE_REGISTRY` is substituted for `__IMAGE_REGISTRY__` in the image defaults; the build fails on any OCID-like value or IPv4 address in the package.

## What gets created

Each resource is created only when its toggle needs it.

| Toggle | Resources |
|---|---|
| `enable_logs` | Log Analytics log group (or reuse `existing_log_group_ocid`); one logs-collector container per enabled cloud |
| `enable_metrics` | metrics bridge (OCI Monitoring `custom_*` namespaces) and one OTel metrics puller per enabled cloud; requires at least one enabled cloud |
| `enable_apm` | APM domain (free tier toggle, or reuse an existing domain), OTLP gateway collector, vault secret with the private data key |
| any cloud | Vault, key, and one placeholder federation secret per cloud |
| any signal | Compartment-scoped IAM; one isolated Container Instance per enabled log cloud, one isolated metrics puller per enabled cloud, and a shared bridge/APM instance when enabled; and (unless `existing_subnet_ocid` is set) a minimal VCN with private subnet, NAT and service gateway |

Cloud inputs are non-secret identifiers only (account, tenant, project, queue, event hub). Authentication to AWS, Azure and GCP uses OIDC federation; never enter keys or secrets as stack variables.

Signal identities are separate stack inputs, including for single-signal deployments:

| Input | Output from matching `mcctl enable` module |
|---|---|
| `aws_logs_role_arn` | AWS logs `role_arn` |
| `aws_metrics_role_arn` | AWS metrics `role_arn` |
| `azure_logs_client_id` | Azure logs `collector_client_id` |
| `azure_metrics_client_id` | Azure metrics `client_id` |

Replace the former shared `aws_role_arn` and `azure_client_id` inputs with the matching per-signal inputs. Defaults are empty; Terraform refuses to render an enabled signal whose AWS role or Azure client is empty or whitespace. Disabled signals need no identity, and an enabled signal never borrows the other signal's identity. Azure tenant and subscription inputs remain shared.

Secrets and the container runtime: Container Instances support only `EMPTYDIR` and `CONFIGFILE` volumes, so vault secrets cannot be mounted. Log collectors resolve `MC_FEDERATION_SECRET_OCID` using the configured resource/instance principal, fetch the CURRENT bundle, and supply a private temporary OIDC token file to the selected cloud SDK. The one exception is the APM private data key for the OTel gateway and APM metrics sink: OTel Collector reads only environment variables, so the key is passed as an environment variable and also exists in the stack state. Restrict who can read the stack state and job outputs.

## Required IAM for the deploying user

Resource Manager runs with the user's permissions. In the target compartment the user needs to manage: `log-analytics-features-family`, `loganalytics-log-group`, `apm-domains`, `vaults`, `keys`, `secret-family`, `virtual-network-family`, `compute-container-family`, `orm-stacks`, `orm-jobs`, `policies`. In the tenancy the user needs to manage `dynamic-groups` and `policies` (both live in the home region). Log Analytics must already be onboarded in the tenancy.

## AWS operator role

Apply `modules/bootstrap/aws-operator-role` once with admin credentials, then run `mcctl` through the least-privilege `mc-operator` profile instead of a broad admin role (see its README).

## Cost notes

- Container Instance: billed per OCPU and GB while it runs (default 2 OCPU, 8 GB). Lower `container_ocpus` / `container_memory_gbs` for light use.
- APM domain: the free tier covers small volumes; production use is billed per span and metric.
- Vault: a default (shared HSM) vault bills per key version and secret; keys and vaults enter a pending-deletion window on removal.
- NAT gateway has no hourly fee; data transfer and cloud-side egress (S3, SQS, Event Hubs, Pub/Sub, cloud metric API calls) are billed by the cloud providers. Keep metric intervals at 5 minutes or more.
- Log Analytics storage is billed beyond the free allowance.

## Remove everything

1. Run `mcctl disable <cloud>` for each cloud first. It stops the collector, removes the cloud-side pipeline in the safe order and asks for a typed confirmation.
2. In Resource Manager, open the stack, choose Destroy, review the plan and confirm the job.

Log Analytics content (parsers, sources) and a reused log group or APM domain are never touched by the stack.

## How mcctl fits

The stack is the OCI half. `mcctl enable <cloud> --signal logs|metrics` creates the matching AWS, Azure or GCP half (queue, event hub or subscription, read-only metric identity, OIDC federation) and prints the identifiers that go into the stack's cloud inputs. Re-apply the stack after changing them.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `400-InvalidParameter, No permissions found` on the policy | A deprecated resource type in a statement (OCI rejects the whole policy without naming it) | The stack uses `loganalytics-log-group`; if you add statements, check each type against the current IAM reference |
| `400-LimitExceeded` creating the vault | Tenancy is at its vault limit; vaults pending deletion still count for 7–30 days | Set **Existing vault** and **Key in the existing vault** in the Vault group, then re-plan |
| Plan works but a cross-field rule fails late | Resource Manager runs Terraform 1.5, which cannot validate one variable against another | Read the precondition message on the failing resource and fix the paired inputs |
| Metrics bridge/puller containers missing | They only run when metrics and at least one cloud are enabled; each cloud uses its own puller instance | Enable metrics and AWS, Azure or GCP in the form |

Historical baseline validation (before MC-28/29): Resource Manager plan → apply (container `CONTAINER_RUNNING`, collector "Everything is ready") → destroy job. This evidence does not validate the new federation runtime or instance layout.


### Vault federation for log collectors (MC-28, local implementation verified; live and release gates open)

For each enabled cloud, replace the stack-created `PLACEHOLDER` secret **out of band** with a raw OIDC JWT issued by the issuer trusted by that cloud's configured federation identity. Store the JWT as BASE64 Vault secret content; never pass it through stack variables, command arguments, Terraform, or logs. An external issuer/controller must keep the CURRENT secret version fresh. The loader retrieves tokens; it cannot mint them or turn OCI resource-principal tokens into cloud-trusted assertions.

The log collector requires more than 90 seconds of remaining JWT lifetime at each read. It retrieves CURRENT every 60 seconds with SDK retries disabled and bounded connect/read timeouts. The file is mode `0600` inside a `0700` temporary directory, replaced atomically, removed after a failed refresh and retried on the next interval. Startup fails with a fixed redacted error for placeholders, malformed/expired assertions, unreadable secrets, mixed token-file/Vault configuration, or non-workload OCI auth. Existing SDK access tokens may remain valid until they expire; removing a file does not revoke them. Destination clouds validate issuer, audience, subject and signature.

Normal exit, exceptions and SIGTERM remove the temporary directory and stop the refresh thread. SIGKILL, host failure and abrupt container destruction cannot run cleanup; use ephemeral container storage and short token lifetimes. Existing projected-token deployments without `MC_FEDERATION_SECRET_OCID` keep their current identity path.

Supply the exact Azure consumer group and Blob checkpoint account/container, or GCP `gcp_logs_wif_provider_resource` (the logs module's `workload_identity_provider_resource` output) and dead-letter topic, in the form. The ORM GCP allow-list is a JSON array of the selected project. Local poison-envelope directories default to `/tmp` and survive only for the container lifetime; durable poison storage remains an operator requirement before production acceptance.

Each log cloud has its own Container Instance, exact-resource dynamic group and policy. The AWS log principal can read only the AWS federation secret; Azure and GCP follow the same rule. Each metrics puller also has a per-cloud Container Instance and exact-secret IAM policy. The bridge and APM gateway share a separate instance with no secret-bundle permission. Each instance uses the configured `container_ocpus` and `container_memory_gbs`, so enabling several clouds and signals multiplies compute capacity and cost. The instance is created before its dynamic group and policy; collectors may restart while IAM propagates. This dependency ordering and the new IAM conditions remain unverified live.

The generated network permits outbound traffic to any destination on all protocols through NAT (or according to the supplied subnet's existing rules). Separate container principals reduce cross-cloud secret access after a workload compromise; they do not prevent a compromised collector from sending data or assertions to an arbitrary egress destination. Destination filtering is recommended defense-in-depth for deployments with stricter egress requirements; owner acceptance of the exposure and applicable network policy remain deployment decisions.

### Vault federation for metric pullers (MC-29, configured; provider and release gates open)

The stack configures the Vault-aware OTel launcher separately for each cloud. It passes only that cloud's SDK environment and assertion secret. Azure reads the launcher-selected token file; GCP receives a temporary external-account credential configuration in a private directory. Set `gcp_metrics_service_account_email` to the `service_account_email` output of `mcctl enable gcp --signal metrics`, and `gcp_metrics_wif_provider_resource` to that module's `workload_identity_provider` output. Each signal uses its own provider input, including when only one signal is enabled; replace the former `gcp_wif_provider_resource` input with the appropriate per-signal input. The GCP credential configuration impersonates this reader account; a missing or malformed account makes the launcher refuse startup. The launcher forwards managed shutdown to the collector and waits for it before removing identity files.

The bridge listens on the private interface so isolated pullers can reach it. For a stack-created subnet, inbound TCP port 4319 is allowed from the configured VCN CIDR only when metric pullers run. A supplied subnet must already permit this traffic; its rules are not changed. This endpoint has no application authentication: subnet access must remain restricted to trusted workloads.

The image source and release workflow are configured locally. No image was built or published during salvage. Cloud token exchange, provider schema/plan validation, IAM propagation and live metrics delivery remain unverified. The existing APM key-in-state exception above remains unchanged; this salvage does not establish production acceptance.
