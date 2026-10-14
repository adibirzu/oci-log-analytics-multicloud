# Tasks

Status: `todo` · `in_progress` · `local_green` (offline tests pass) · `live_green` (proven against a real cloud). **S** = senior review.

| ID | Task | Files | Acceptance | Status |
|---|---|---|---|---|
| MC-01 | Scaffold: `pyproject.toml`, `requirements*.txt`, pytest config, ruff | root | `pytest` green offline | local_green |
| MC-02 **S** | Move log collector to `collectors/logs`, content to `content/logan`, catalog alongside; fix imports and test paths | `collectors/logs`, `content/logan`, `tests/` | all seeded ingest tests pass unchanged in behaviour | local_green |
| MC-03 | Move per-cloud log Terraform to `modules/logs/{aws,azure,gcp}/terraform`; plan-summary golden tests | `modules/logs` | fmt clean; goldens match | local_green |
| MC-04 | `mcctl/state.py`: backend key derivation, manifest, lock | `mcctl/` | tmp-dir fakes, concurrent lock test | local_green |
| MC-05 **S** | `mcctl/plan.py`: injected terraform runner, `show -json` summary, plan SHA256, stale-plan refusal | `mcctl/` | fake runner replays recorded JSON | local_green |
| MC-06 | `mcctl enable|status` for logs on all clouds | `mcctl/cli.py` | second enable = empty plan | local_green |
| MC-07 **S** | `mcctl/teardown/aws.py`: collector stop → targeted trail+policy destroy → empty bucket → destroy | `mcctl/teardown` | deny policy present ⇒ empty fails; `force_destroy` stays false | local_green |
| MC-08 | `teardown/azure.py`, `teardown/gcp.py` ordered teardown | `mcctl/teardown` | fakes prove order | local_green |
| MC-09 | `mcctl disable` wiring, `--confirm-destroy <account>`, protected-resource report | `mcctl/cli.py` | refuses without confirmation | local_green |
| MC-10 **S** | `collectors/metrics_bridge/mapping.py` namespace/name/dimension/cardinality/timestamp rules | bridge | table goldens incl. `oci_` prefix, >20 dims, stale points | local_green |
| MC-11 | `metrics_bridge/otlp_decode.py`: OTLP protobuf → points; cumulative → delta | bridge | Gauge/Sum/Histogram fixtures | local_green |
| MC-12 **S** | `metrics_bridge/post.py`: 50-stream batches, token bucket, retry, `failedMetrics` | bridge | 120 streams ⇒ 3 calls; 429 retried | local_green |
| MC-13 | Bridge HTTP server (`/v1/metrics`), self-metrics, Dockerfile | bridge | POST fixture ⇒ fake client payload | local_green |
| MC-14 | `collectors/otel/metrics-pull` generator for the three receivers from config | `collectors/otel` | schema + golden; no static keys | local_green |
| MC-15 **S** | Metrics reader Terraform per cloud (read-only, OIDC) | `modules/metrics` | plan goldens; no `*` actions | local_green |
| MC-16 | APM gateway collector config → OCI APM (data key from env, sampling, health-check drop) | `collectors/otel` | lint + golden + secret grep | local_green |
| MC-17 | APM onboarding docs + sample apps per cloud | `modules/apm`, `examples/` | sample exports to local fake | local_green |
| MC-18 | VM exporter path from `metrics/prometheus` → `modules/metrics/vm-exporters`; bash test + shellcheck | `modules/metrics/vm-exporters` | bash test passes | local_green |
| MC-19 | LA content loader (`scripts/upsert_logan_content.py`) diff/dry-run on `content/logan` | `scripts/` | create/update/no-op with fake client | local_green |
| MC-20 **S** | Redaction gate: pre-commit grep + fixture scan test | `scripts/`, `tests/` | fails on seeded real-looking values | local_green |

## Next (live, owner-approved per cloud)

| ID | Task | Status |
|---|---|---|
| MC-21 | `mcctl enable aws --signal logs` live round trip (plan review → apply → collector → LA query → `mcctl disable`) | live_green (3 files uploaded, 93 records queried, clean removal) |
| MC-22 | Metrics bridge live post to OCI Monitoring from a CloudWatch pull; confirm namespace/dimension rules | todo |
| MC-23 | APM gateway live: sample app → gateway → OCI APM; settle data-key type and metrics endpoint | todo |
| MC-24 | Azure and GCP live round trips (needs owner subscription/project + federation) | todo |
| MC-25 | Remote state backend option (OCI Object Storage S3-compatible) | todo |
