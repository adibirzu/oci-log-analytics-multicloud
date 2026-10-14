"""Minimal Flask app instrumented with the OpenTelemetry SDK.

Emits one trace per request (Flask server span plus a custom child span) and
one counter metric. Telemetry goes over OTLP/HTTP to the APM gateway collector
(`collectors/otel/apm-gateway.yaml`), which forwards it to OCI APM.

Environment:
  OTEL_EXPORTER_OTLP_ENDPOINT  base URL of the gateway (default http://localhost:4318)
  OTEL_SERVICE_NAME            service.name resource attribute (default example-flask)
  CLOUD_PROVIDER               cloud.provider resource attribute: aws | azure | gcp
  OTEL_METRIC_EXPORT_INTERVAL_MS  metric export period (default 60000)

No credentials are read here. The OCI APM data key lives in the gateway only.
"""

import os
from dataclasses import dataclass

from flask import Flask, jsonify
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.metrics import MeterProvider
from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import TracerProvider as ApiTracerProvider

DEFAULT_ENDPOINT = "http://localhost:4318"
DEFAULT_SERVICE = "example-flask"
DEFAULT_CLOUD = "unknown"
DEFAULT_METRIC_INTERVAL_MS = 60000


@dataclass
class Telemetry:
    tracer_provider: ApiTracerProvider
    meter_provider: MeterProvider
    request_counter: object


def _resource(service_name: str, cloud_provider: str) -> Resource:
    return Resource.create(
        {
            "service.name": service_name,
            "cloud.provider": cloud_provider,
        }
    )


def build_telemetry(
    endpoint: str | None = None,
    service_name: str | None = None,
    cloud_provider: str | None = None,
    metric_interval_ms: int | None = None,
) -> Telemetry:
    """Create providers without touching OTel globals, so tests and apps can hold several."""
    base = (endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or DEFAULT_ENDPOINT).rstrip("/")
    service = service_name or os.getenv("OTEL_SERVICE_NAME") or DEFAULT_SERVICE
    cloud = cloud_provider or os.getenv("CLOUD_PROVIDER") or DEFAULT_CLOUD
    interval = metric_interval_ms or int(
        os.getenv("OTEL_METRIC_EXPORT_INTERVAL_MS", str(DEFAULT_METRIC_INTERVAL_MS))
    )
    resource = _resource(service, cloud)

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces")))

    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{base}/v1/metrics"),
        export_interval_millis=interval,
    )
    meter_provider = SdkMeterProvider(resource=resource, metric_readers=[reader])
    counter = meter_provider.get_meter("example-flask").create_counter(
        name="example.requests",
        unit="{request}",
        description="Requests handled by the example app",
    )
    return Telemetry(tracer_provider=tracer_provider, meter_provider=meter_provider, request_counter=counter)


def create_app(telemetry: Telemetry | None = None) -> Flask:
    telemetry = telemetry or build_telemetry()
    app = Flask(__name__)
    FlaskInstrumentor().instrument_app(
        app,
        tracer_provider=telemetry.tracer_provider,
        meter_provider=telemetry.meter_provider,
    )
    tracer = telemetry.tracer_provider.get_tracer("example-flask")

    @app.get("/")
    def index():
        telemetry.request_counter.add(1, {"route": "/"})
        with tracer.start_as_current_span("build-greeting") as span:
            span.set_attribute("greeting.language", "en")
            body = {"message": "hello from the example app"}
        return jsonify(body)

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    return app


if __name__ == "__main__":  # pragma: no cover - manual run only
    telemetry = build_telemetry()
    flask_app = create_app(telemetry)
    try:
        flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
    finally:
        telemetry.tracer_provider.shutdown()
        telemetry.meter_provider.shutdown()
