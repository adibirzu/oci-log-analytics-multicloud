"""Offline check of the Flask APM example: spans and metrics arrive at an OTLP/HTTP receiver.

A stdlib HTTP server on an ephemeral port stands in for the APM gateway. No network
beyond 127.0.0.1, no cloud calls.
"""

import importlib.util
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("flask")
pytest.importorskip("opentelemetry.sdk.trace")
pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
pytest.importorskip("opentelemetry.instrumentation.flask")
pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")
pytest.importorskip("opentelemetry.proto.collector.metrics.v1.metrics_service_pb2")

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest  # noqa: E402
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest  # noqa: E402

APP_PATH = Path(__file__).resolve().parents[1] / "examples" / "apm" / "python-flask" / "app.py"


def _load_app_module():
    spec = importlib.util.spec_from_file_location("example_flask_app", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Receiver:
    """Records every OTLP/HTTP POST as (path, content_type, body)."""

    def __init__(self):
        self.posts = []
        self._lock = threading.Lock()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - stdlib naming
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                with receiver._lock:
                    receiver.posts.append((self.path, self.headers.get("Content-Type"), body))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):  # keep test output quiet
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    def bodies(self, path):
        with self._lock:
            return [body for p, _, body in self.posts if p == path]


@pytest.fixture
def receiver():
    with _Receiver() as rx:
        yield rx


def _resource_attrs(resource):
    return {kv.key: kv.value.string_value for kv in resource.attributes}


def _exercise_app(module, telemetry):
    app = module.create_app(telemetry)
    response = app.test_client().get("/")
    assert response.status_code == 200
    assert response.get_json() == {"message": "hello from the example app"}
    telemetry.tracer_provider.force_flush()
    telemetry.meter_provider.force_flush()


def _shutdown(telemetry):
    telemetry.tracer_provider.shutdown()
    telemetry.meter_provider.shutdown()


def test_request_exports_spans_and_metric_over_otlp_http(receiver):
    module = _load_app_module()
    telemetry = module.build_telemetry(
        endpoint=receiver.base_url,
        service_name="orders-api",
        cloud_provider="azure",
        metric_interval_ms=60000,
    )
    try:
        _exercise_app(module, telemetry)
    finally:
        _shutdown(telemetry)

    trace_bodies = receiver.bodies("/v1/traces")
    metric_bodies = receiver.bodies("/v1/metrics")
    assert trace_bodies, "no POST to /v1/traces"
    assert metric_bodies, "no POST to /v1/metrics"

    spans, resources = [], []
    for body in trace_bodies:
        request = ExportTraceServiceRequest.FromString(body)
        for resource_spans in request.resource_spans:
            resources.append(_resource_attrs(resource_spans.resource))
            for scope in resource_spans.scope_spans:
                spans.extend(span.name for span in scope.spans)

    assert "build-greeting" in spans, spans
    assert any(name.startswith("GET") for name in spans), spans
    assert all(r.get("service.name") == "orders-api" for r in resources), resources
    assert all(r.get("cloud.provider") == "azure" for r in resources), resources

    # Cumulative temporality: each export carries the running total, so the last one wins.
    metric_names, counter_values = [], []
    for body in metric_bodies:
        request = ExportMetricsServiceRequest.FromString(body)
        for resource_metrics in request.resource_metrics:
            assert _resource_attrs(resource_metrics.resource)["service.name"] == "orders-api"
            for scope in resource_metrics.scope_metrics:
                for metric in scope.metrics:
                    metric_names.append(metric.name)
                    if metric.name == "example.requests":
                        counter_values.append(sum(point.as_int for point in metric.sum.data_points))
    assert "example.requests" in metric_names, metric_names
    assert counter_values[-1] == 1, counter_values


def test_endpoint_and_resource_attributes_come_from_environment(receiver, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", receiver.base_url)
    monkeypatch.setenv("OTEL_SERVICE_NAME", "env-service")
    monkeypatch.setenv("CLOUD_PROVIDER", "gcp")
    module = _load_app_module()
    telemetry = module.build_telemetry(metric_interval_ms=60000)
    try:
        _exercise_app(module, telemetry)
    finally:
        _shutdown(telemetry)

    trace_bodies = receiver.bodies("/v1/traces")
    assert trace_bodies, "OTEL_EXPORTER_OTLP_ENDPOINT was not honored"
    request = ExportTraceServiceRequest.FromString(trace_bodies[0])
    attrs = _resource_attrs(request.resource_spans[0].resource)
    assert attrs["service.name"] == "env-service"
    assert attrs["cloud.provider"] == "gcp"
