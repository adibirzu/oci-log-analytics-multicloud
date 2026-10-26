# Architecture

Centralize AWS, Azure and GCP telemetry in OCI. Every cloud integration is
enabled and disabled on demand with `mcctl`, so idle stacks cost nothing.

| Signal | Destination | AWS | Azure | GCP |
|---|---|---|---|---|
| Logs | OCI Log Analytics (direct `upload_log_events_file`) | CloudTrail → S3 → SQS → collector (live-proven) | Diagnostic settings → Event Hubs → collector | Log sink → Pub/Sub → collector |
| Metrics (pull) | OCI Monitoring via `metrics_bridge` (PostMetricData) | OTel contrib `awscloudwatchreceiver` | `azuremonitorreceiver` | `googlecloudmonitoringreceiver` |
| VM / K8s metrics | OCI Monitoring (Management Agent) or OTLP | Prometheus exporters (`modules/metrics/vm-exporters`) | same | same |
| Traces / APM | OCI APM (OTLP) | ADOT / OTel SDK → gateway collector | Azure Monitor OTel distro / OTel SDK → gateway | OTel SDK / Ops Agent → gateway |

## Layout

```
mcctl/                          CLI: enable | disable | status <cloud> [--signal]
  cli.py plan.py state.py preflight.py teardown/{aws,azure,gcp}.py
modules/
  logs/{aws,azure,gcp}/terraform/      log pipeline infrastructure per cloud
  metrics/{aws,azure,gcp}/terraform/   read-only metric reader identities
  metrics/vm-exporters/                Prometheus exporters + OTel installer (from oci-prometheus-otel-monitoring)
  apm/{aws,azure,gcp}/                 onboarding docs + collector snippets
collectors/
  logs/                         multi-cloud log collector (package `collectors.logs`)
  metrics_bridge/               OTLP metrics → OCI Monitoring PostMetricData
  otel/                         contrib collector configs (metrics-pull, apm-gateway)
content/logan/                  Log Analytics sources, parsers, fields as code
tests/                          offline only; fakes for every cloud
```

## Verified facts (official docs, 2026-10-14)

- OCI APM accepts OTLP spans **and metrics**: `<dataUploadEndpoint>/20200101/observations/{private-span|public-span|metric}?dataFormat={otlp-span|otlp-metric}&dataFormatVersion=1` with a data key. Field-verified: OTLP traces accept `Authorization: dataKey <key>` on `/20200101/opentelemetry/private/v1/traces`.
- OCI Monitoring ingest: `PostMetricData` on `telemetry-ingestion.<region>.oraclecloud.com`; signed requests; namespace must not start with `oci_`/`oracle_`; points < 2 h old and < 10 min in the future; ≤ 50 metric streams per call; 1–20 dimensions; 50 TPS per tenancy. No maintained OTel exporter for OCI Monitoring was found, hence `metrics_bridge`.
- Contrib receivers (all alpha): `awscloudwatchreceiver` (GetMetricData, SDK credential chain), `azuremonitorreceiver` (workload/managed identity, `azureauth`), `googlecloudmonitoringreceiver` (ADC). There is no `awscloudwatchmetricsreceiver`.

## Assumptions to verify live

- Which APM key type (private/public) the `metric` observation requires; protobuf vs JSON body.
- PostMetricData max request size; histogram handling (emit count/sum/min/max as separate metrics).

## metrics_bridge mapping rules

- Namespace `custom_<cloud>_<service>`, lowercase, `[a-z0-9_]` only, never `oci_`/`oracle_`.
- Metric name sanitized; unit to `metadata.unit`.
- Dimensions from `cloud.provider`, `cloud.account.id`, `cloud.region`, `service.name`, `cloud.resource_id` (hashed) + point attributes; max 20, high-cardinality keys dropped first in fixed priority.
- Cardinality cap (default 5,000 streams/namespace/interval) with `dropped_total{reason}` self-metric.
- Gauge as-is; cumulative Sum → delta; Histogram → `_count`, `_sum`, `_min`, `_max`; stale/future points dropped and counted.
- Batches of ≤ 50 streams, token bucket under 50 TPS, jittered retry on 429/5xx, `failedMetrics` parsed.
- `metrics_sink: monitoring | apm | both` (APM-only needs no custom code but has no Monitoring alarms).

## Identity

- Cloud side: OIDC federation only (AWS web identity role, Azure federated credential, GCP workload identity pool). Metric readers are read-only (`cloudwatch:GetMetricData/ListMetrics`, `Monitoring Reader`, `roles/monitoring.viewer`).
- OCI side: resource or instance principal; policies scoped to one compartment. Operator API-key auth only with `INGEST_ALLOW_OPERATOR_AUTH=true` for supervised tests.
- Vault-backed cloud assertions use private temporary files; see [the deployment guide](DEPLOY.md#vault-federation-for-log-collectors-mc-28-local-implementation-verified-live-and-release-gates-open) for rotation and isolation. The APM data-key handling and its Terraform-state exception are documented in [Secrets and the container runtime](DEPLOY.md#what-gets-created).

## Enable / disable (`mcctl`)

- State per cloud, signal and account: `state/<cloud>/<account>/<signal>.tfstate`; lock and manifest under `~/.mcctl/`. State is local by default (`backend "local" {}` in each module, path passed via `-backend-config=path=` under `~/.mcctl/state/`); a remote backend is a later option.
- `enable`: preflight (OCI tenancy/profile/compartment, cloud account from config) → `terraform plan -out` → summary (add/change/destroy, IAM changes, must be zero destroys) → manifest with plan SHA256 → stop for review unless `--yes` → apply the saved plan → status.
- Idempotent: re-enable gives an empty plan; disable of absent state exits 0; stale plans (state serial changed) are refused.
- `disable` ordering (lessons from the 2026-10-14 AWS teardown):
  1. stop the collector;
  2. targeted destroy of the CloudTrail trail and the bucket policy (it explicitly denies object deletes);
  3. empty the bucket (versions and delete markers) — `force_destroy` stays `false`;
  4. destroy plan → review → apply; typed `--confirm-destroy <account>`.
  Azure: consumer groups before the Event Hubs namespace. GCP: subscription before topic. Shared OIDC providers are guarded with `count` / `existing_provider_arn`. OCI-side content (log groups, parsers) is never removed unless `--include-oci`.

## Cost knobs

Log retention, CloudTrail data events off by default, collector batch limits; metric allowlists and ≥ 5-minute intervals (cloud metric APIs bill per request); APM head sampling and health-check span drop.
