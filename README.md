# oci-log-analytics-multicloud

[![Deploy to Oracle Cloud](https://oci-resourcemanager-plugin.plugins.oci.oraclecloud.com/latest/deploy-to-oracle-cloud.svg)](https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=https://github.com/adibirzu/oci-log-analytics-multicloud/releases/latest/download/oci-log-analytics-multicloud-orm.zip)

Centralize AWS, Azure and GCP telemetry in Oracle Cloud Infrastructure:

| Signal | Destination | Status |
|---|---|---|
| Logs | OCI Log Analytics | AWS CloudTrail live-proven; Azure Event Hubs and GCP Pub/Sub collectors ported |
| Metrics | OCI Monitoring | Bridge and cloud-native pull configurations implemented locally; live monitoring acceptance pending (MC-22) |
| Traces / APM | OCI APM (OTLP) | Gateway and ORM integration implemented locally; live end-to-end acceptance pending (MC-23) |

Every cloud integration can be enabled and disabled on demand so idle test stacks cost nothing.

## Quick start

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e '.[dev,aws]'
cp mcctl.example.yaml mcctl.yaml                        # ${VAR} references resolve from env, never literals
.venv/bin/mcctl enable aws --signal logs --plan-only    # review the saved plan
.venv/bin/mcctl enable aws --signal logs --yes          # apply exactly that plan
.venv/bin/mcctl status aws
.venv/bin/mcctl disable aws --signal logs --dry-run     # ordered removal preview
.venv/bin/mcctl disable aws --signal logs --confirm-destroy <account-id>
```

State lives under `~/.mcctl/state/<cloud>/<account>/<signal>.tfstate`. See `docs/ARCHITECTURE.md` and `docs/TASKS.md`.

## Provenance

- `collectors/logs/`, `modules/logs/{aws,azure,gcp}/terraform`, `content/logan/` — multi-cloud log collector, per-cloud Terraform and Log Analytics content.
- `modules/metrics/vm-exporters/` — from `oci-prometheus-otel-monitoring` (UPL-1.0).

No tenant values are committed: OCIDs, account IDs, IPs, namespaces and keys are variables or `<PLACEHOLDER>` tokens.
