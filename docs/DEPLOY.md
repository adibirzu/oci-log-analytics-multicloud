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
| `enable_metrics` | metrics bridge (OCI Monitoring `custom_*` namespaces) and an OTel metrics-pull collector |
| `enable_apm` | APM domain (free tier toggle, or reuse an existing domain), OTLP gateway collector, vault secret with the private data key |
| any cloud | Vault, key, and one placeholder federation secret per cloud |
| any signal | Dynamic group and compartment-scoped policy, a Container Instance, and (unless `existing_subnet_ocid` is set) a minimal VCN with a private subnet, NAT and service gateway |

Cloud inputs are non-secret identifiers only (account, tenant, project, queue, event hub). Authentication to AWS, Azure and GCP uses OIDC federation; never enter keys or secrets as stack variables.

Secrets and the container runtime: Container Instances support only `EMPTYDIR` and `CONFIGFILE` volumes, so vault secrets cannot be mounted. Collectors get the secret OCID in `MC_FEDERATION_SECRET_OCID` and must read it with the instance's resource principal. The one exception is the APM private data key for the OTel gateway and APM metrics sink: OTel Collector reads only environment variables, so the key is passed as an environment variable and also exists in the stack state. Restrict who can read the stack state and job outputs.

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
| Metrics bridge/puller containers missing | They only run when at least one cloud is enabled | Enable AWS, Azure or GCP in the form |

Tested end to end with Resource Manager: plan → apply (container `CONTAINER_RUNNING`, collector "Everything is ready") → destroy job.
