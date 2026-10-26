# Collector configs (`collectors/otel`)

Contrib OpenTelemetry Collector configs that move cloud metrics and workload
telemetry into OCI.

- `generate.py` renders the metrics-pull config (AWS CloudWatch, Azure Monitor,
  GCP Cloud Monitoring receivers) from a reviewed allowlist. Input schema:
  `metrics_pull.schema.json`; example: `metrics-pull.example.yaml`.
  Run: `python -m collectors.otel.generate --input metrics-pull.yaml --output collector.yaml`.
- `apm-gateway.yaml` is a static gateway: OTLP in (4317/4318), health-check span
  drop, probabilistic sampling, export to OCI APM. Set `OCI_APM_ENDPOINT`,
  `OCI_APM_PRIVATE_DATAKEY`, and optionally `APM_SAMPLING_PERCENT` (default 10).

## Rules

- No static cloud keys. AWS uses the SDK chain or web identity, Azure uses
  workload or managed identity, GCP uses ADC or workload identity.
- Generated configs reference secret-like values through `${env:NAME}`. The generator refuses literal values
  for keys named like `access_key`, `secret`, `client_secret`, `password`, `token`.
- Metric polls are at least 300 s (cloud metric APIs bill per request). Metrics
  come from an explicit allowlist; an empty allowlist is rejected.

## Verified facts (official docs, 2026-10-14; see `docs/ARCHITECTURE.md`)

- Contrib receivers exist: `awscloudwatchreceiver`, `azuremonitorreceiver`,
  `googlecloudmonitoringreceiver`. All are alpha.
- OCI APM OTLP traces path `/20200101/opentelemetry/private/v1/traces` with
  `Authorization: dataKey <key>`.

## Verified against otelcol-contrib 0.162.0

Tested with `otelcol-contrib validate --config <golden>` (exit 0) for both
goldens and `apm-gateway.yaml`. Run it with `OTELCOL_BIN=/path/to/otelcol-contrib
python3.11 -m pytest tests/test_otel_generate.py`; the tests are skipped without it.

- `awscloudwatch`: `region`; `metrics.collection_interval`, `metrics.period`;
  `metrics.queries[]` with `namespace`, `metric_name`, `stats`, optional
  `dimensions` (map). Rejected: `poll_interval`, `interval`, `period` at top level,
  `stat`, `statistics`, `named`.
- `azuremonitor`: `subscription_ids`, `collection_interval`, `services`,
  `metrics: {<service>: {default: [<metric>]}}`, and `auth: {authenticator: azureauth}`
  with the `azureauth` extension (`managed_identity: {}` or
  `workload_identity: {tenant_id, client_id, federated_token_file}`). A bare
  `auth: <string>` is rejected. The `default` map key is what the validator accepts;
  its semantics (aggregation or category) are not checked.
- `googlecloudmonitoring`: `project_id`, `collection_interval`,
  `metrics_list: [{metric_name}]`; no extension needed (ADC).
- The generated `azureauth` workload-identity file path is
  `${env:AZURE_FEDERATED_TOKEN_FILE}`. Supply that environment variable with the
  projected token path when running without the Vault launcher. For Vault-backed
  pullers, follow [the deployment guide](../../docs/DEPLOY.md#vault-federation-for-metric-pullers-mc-29-configured-provider-and-release-gates-open).
  This path substitution is covered by
  `tests/test_otel_generate.py::test_azure_workload_identity_uses_launcher_selected_file`;
  the earlier binary validation above does not validate this workload-identity path.

## Unverified

- Metrics endpoint `/20200101/opentelemetry/v1/metrics` and which APM key type
  (private or public) it needs. ARCHITECTURE.md lists this as an assumption.
  `validate` does not contact the endpoint, so this is not covered by the binary check.
- Whether the collector's `${env:NAME:-default}` form is honored for
  `APM_SAMPLING_PERCENT`. `validate` passed with the variable set explicitly; the
  default path is not exercised.
- Whether the receivers start and scrape successfully. `validate` checks config
  shape only.
