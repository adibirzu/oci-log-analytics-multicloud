import gzip
import http.client
import threading
from datetime import datetime, timedelta, timezone

import pytest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
    ExportMetricsServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.metrics.v1 import metrics_pb2 as pb

from collectors.metrics_bridge import server as srv
from collectors.metrics_bridge.model import DropReason

COMP = "<COMPARTMENT_OCID>"
CONFIG = srv.BridgeConfig(region="example-region-1", compartment_id=COMP)
NOW = datetime.now(timezone.utc)
NS_NOW = int(NOW.timestamp() * 1e9)


class FakePoster:
    def __init__(self):
        self.points = []

    def post(self, points):
        self.points.extend(points)


@pytest.fixture
def bridge():
    poster = FakePoster()
    server = srv.build_server(CONFIG, poster=poster, listen=("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, poster
    finally:
        server.shutdown()
        server.server_close()


def kv(k, v):
    return KeyValue(key=k, value=AnyValue(string_value=v))


def otlp_bytes(name="reqs", value=3.0, ts_ns=NS_NOW, service="ec2"):
    req = ExportMetricsServiceRequest()
    rm = req.resource_metrics.add()
    rm.resource.attributes.extend([kv("cloud.provider", "aws"), kv("service.name", service),
                                   kv("cloud.region", "r1")])
    dp = pb.NumberDataPoint(time_unix_nano=ts_ns, as_double=value)
    dp.attributes.extend([kv("host", "h1")])
    gauge = pb.Gauge(data_points=[dp])
    rm.scope_metrics.add().metrics.append(pb.Metric(name=name, unit="1", gauge=gauge))
    return req.SerializeToString()


def send(server, body, ctype="application/x-protobuf", headers=None, method="POST", path="/v1/metrics"):
    host, port = server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=10)
    hdrs = {"Content-Type": ctype} if ctype is not None else {}
    hdrs.update(headers or {})
    conn.request(method, path, body=body, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read()
    out = resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
    conn.close()
    return out


def test_protobuf_post_reaches_poster_with_mapped_points(bridge):
    server, poster = bridge
    status, headers, body = send(server, otlp_bytes(value=3.0))
    assert status == 200
    assert headers["content-type"] == "application/x-protobuf"
    assert ExportMetricsServiceResponse.FromString(body).partial_success.rejected_data_points == 0
    (p,) = poster.points
    assert (p.namespace, p.name, p.value, p.unit) == ("custom_aws_ec2", "reqs", 3.0, "1")
    assert dict(p.dimensions)["host"] == "h1"


def test_gzip_body_is_decoded(bridge):
    server, poster = bridge
    status, _, _ = send(server, gzip.compress(otlp_bytes(name="gz")),
                        headers={"Content-Encoding": "gzip"})
    assert status == 200
    assert [p.name for p in poster.points] == ["gz"]


def test_gzip_bomb_is_413(bridge):
    server, poster = bridge
    bomb = gzip.compress(b"\x00" * (srv.MAX_BODY_BYTES + 1024))
    status, _, _ = send(server, bomb, headers={"Content-Encoding": "gzip"})
    assert status == 413
    assert poster.points == []


def test_oversized_body_is_413_without_reading(bridge):
    server, poster = bridge
    status, _, _ = send(server, None, headers={"Content-Length": str(srv.MAX_BODY_BYTES + 1)})
    assert status == 413
    assert poster.points == []


def test_json_is_415(bridge):
    server, poster = bridge
    status, _, body = send(server, b'{"resourceMetrics": []}', ctype="application/json")
    assert status == 415
    assert b"JSON" in body and poster.points == []


def test_unknown_content_encoding_is_415(bridge):
    server, _ = bridge
    status, _, _ = send(server, otlp_bytes(), headers={"Content-Encoding": "br"})
    assert status == 415


def test_malformed_protobuf_is_400_without_echoing_body(bridge):
    server, poster = bridge
    status, _, body = send(server, b"\xff\xff not protobuf \x00")
    assert status == 400
    assert b"not protobuf" not in body and poster.points == []


def test_unknown_path_is_404(bridge):
    server, _ = bridge
    status, _, _ = send(server, otlp_bytes(), path="/v1/traces")
    assert status == 404


def test_healthz(bridge):
    server, _ = bridge
    host, port = server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", "/healthz")
    resp = conn.getresponse()
    assert resp.status == 200 and resp.read() == b"ok\n"
    conn.close()


def test_metrics_exposes_counters_and_drop_reasons(bridge):
    server, _ = bridge
    stale = otlp_bytes(ts_ns=NS_NOW - int(timedelta(hours=3).total_seconds() * 1e9))
    status, _, body = send(server, stale)
    assert status == 200
    rejected = ExportMetricsServiceResponse.FromString(body).partial_success.rejected_data_points
    assert rejected == 1  # stale point rejected and reported back to the sender

    host, port = server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", "/metrics")
    resp = conn.getresponse()
    text = resp.read().decode()
    conn.close()
    assert resp.status == 200
    assert 'bridge_dropped_points_total{reason="stale"} 1' in text
    assert 'bridge_dropped_points_total{reason="future"} 0' in text
    assert "bridge_posted_streams_total 0" in text
    assert "bridge_failed_streams_total 0" in text
    assert "bridge_post_batches_total 0" in text


def test_metrics_output_never_contains_dimension_values(bridge):
    server, _ = bridge
    send(server, otlp_bytes(name="secretname"))
    host, port = server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", "/metrics")
    text = conn.getresponse().read().decode()
    conn.close()
    assert "h1" not in text and "secretname" not in text


def test_default_listen_is_loopback():
    cfg = srv.BridgeConfig.from_env({"OCI_REGION": "example-region-1", "OCI_COMPARTMENT_ID": COMP})
    assert (cfg.listen_host, cfg.listen_port) == ("127.0.0.1", 4319)
    assert cfg.auth_mode == "resource_principal"
    assert srv.parse_listen("0.0.0.0:5000") == ("0.0.0.0", 5000)


def test_env_config_requires_region_and_compartment():
    with pytest.raises(srv.ConfigError):
        srv.BridgeConfig.from_env({"OCI_COMPARTMENT_ID": COMP})
    with pytest.raises(srv.ConfigError):
        srv.BridgeConfig.from_env({"OCI_REGION": "example-region-1"})


def test_monitoring_compartment_fallback_and_defaults():
    cfg = srv.BridgeConfig.from_env({"OCI_REGION": "example-region-1",
                                     "MONITORING_COMPARTMENT_ID": COMP,
                                     "BRIDGE_DEFAULT_CLOUD": "azure",
                                     "BRIDGE_DEFAULT_SERVICE": "aks"})
    assert cfg.compartment_id == COMP
    assert cfg.resource_defaults() == {"cloud.provider": "azure", "service.name": "aks"}


def test_operator_auth_needs_explicit_opt_in():
    env = {"OCI_REGION": "example-region-1", "OCI_COMPARTMENT_ID": COMP, "OCI_AUTH_MODE": "config"}
    with pytest.raises(srv.ConfigError):
        srv.BridgeConfig.from_env(env)
    env["INGEST_ALLOW_OPERATOR_AUTH"] = "true"
    assert srv.BridgeConfig.from_env(env).auth_mode == "config"
    with pytest.raises(srv.ConfigError):
        srv.BridgeConfig.from_env({**env, "OCI_AUTH_MODE": "bogus"})


def test_default_resource_attributes_used_when_sender_omits_them(bridge):
    server, poster = bridge
    req = ExportMetricsServiceRequest()
    rm = req.resource_metrics.add()
    dp = pb.NumberDataPoint(time_unix_nano=NS_NOW, as_double=1.0)
    rm.scope_metrics.add().metrics.append(pb.Metric(name="g", gauge=pb.Gauge(data_points=[dp])))
    server.decoder.defaults = {"cloud.provider": "gcp", "service.name": "gke"}
    status, _, _ = send(server, req.SerializeToString())
    assert status == 200
    assert poster.points[0].namespace == "custom_gcp_gke"
