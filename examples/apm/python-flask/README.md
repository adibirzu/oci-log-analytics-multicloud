# Flask APM example

A minimal Flask app that emits one trace per request (server span plus a custom
`build-greeting` child span) and one counter metric (`example.requests`). Telemetry
goes over OTLP/HTTP to the APM gateway (`collectors/otel/apm-gateway.yaml`).

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` | Base URL of the gateway. `/v1/traces` and `/v1/metrics` are appended. |
| `OTEL_SERVICE_NAME` | `example-flask` | `service.name` resource attribute. |
| `CLOUD_PROVIDER` | `unknown` | `cloud.provider` resource attribute: `aws`, `azure` or `gcp`. |
| `OTEL_METRIC_EXPORT_INTERVAL_MS` | `60000` | Metric export period. |
| `PORT` | `8080` | Port for `python app.py`. |

No cloud credentials or OCI keys are read by the app.

## Run locally

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318   # your gateway or a local collector
export OTEL_SERVICE_NAME=orders-api CLOUD_PROVIDER=aws
python app.py
curl -s http://localhost:8080/
```

## Routes

- `GET /` returns a greeting, adds to `example.requests`, and opens the `build-greeting` span.
- `GET /health` returns `{"status": "ok"}`. The gateway drops health-check spans.

## Test

The offline test runs this app against a fake OTLP/HTTP receiver:

```bash
.venv/bin/python -m pytest tests/test_apm_examples.py -q
```

It skips cleanly if Flask or the OpenTelemetry packages are not installed.
