"""HTTP front for the metrics bridge.

POST /v1/metrics  OTLP ExportMetricsServiceRequest (application/x-protobuf,
                  optional gzip Content-Encoding) -> OCI Monitoring PostMetricData
GET  /healthz     liveness
GET  /metrics     Prometheus text of the bridge's own counters

Only the standard library HTTP server is used. Request bodies, dimension values
and attribute contents are never logged.
"""
from __future__ import annotations

import logging
import os
import threading
import zlib
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Mapping, Optional, Tuple

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceResponse,
)

from .model import DropReason
from .otlp_decode import OtlpDecoder
from .post import MonitoringPoster, build_monitoring_client
from .stats import BridgeStats

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 8 * 1024 * 1024  # compressed and decompressed body limit
PROTOBUF_CONTENT_TYPE = "application/x-protobuf"
METRICS_PATH = "/v1/metrics"
DEFAULT_LISTEN = "127.0.0.1:4319"
AUTH_MODES = ("resource_principal", "instance_principal", "config")
_NOT_COUNTED_AS_REJECTED = frozenset({DropReason.too_many_dims})  # point kept, dims trimmed


class ConfigError(ValueError):
    """Raised for missing or inconsistent bridge configuration."""


def parse_listen(value: str) -> Tuple[str, int]:
    host, sep, port = (value or DEFAULT_LISTEN).rpartition(":")
    if not sep or not port.isdigit():
        raise ConfigError("BRIDGE_LISTEN must be host:port")
    return host or "127.0.0.1", int(port)


@dataclass(frozen=True)
class BridgeConfig:
    region: str
    compartment_id: str
    auth_mode: str = "resource_principal"
    config_file: Optional[str] = None
    profile: str = "DEFAULT"
    listen_host: str = "127.0.0.1"
    listen_port: int = 4319
    default_cloud: Optional[str] = None
    default_service: Optional[str] = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "BridgeConfig":
        region = env.get("OCI_REGION", "").strip()
        if not region:
            raise ConfigError("OCI_REGION is required")
        compartment = (env.get("OCI_COMPARTMENT_ID") or env.get("MONITORING_COMPARTMENT_ID") or "").strip()
        if not compartment:
            raise ConfigError("OCI_COMPARTMENT_ID (or MONITORING_COMPARTMENT_ID) is required")
        auth = env.get("OCI_AUTH_MODE", "resource_principal").strip() or "resource_principal"
        if auth not in AUTH_MODES:
            raise ConfigError(f"OCI_AUTH_MODE must be one of {', '.join(AUTH_MODES)}")
        if auth == "config" and env.get("INGEST_ALLOW_OPERATOR_AUTH", "").lower() != "true":
            raise ConfigError("OCI_AUTH_MODE=config requires INGEST_ALLOW_OPERATOR_AUTH=true")
        host, port = parse_listen(env.get("BRIDGE_LISTEN", DEFAULT_LISTEN))
        return cls(
            region=region,
            compartment_id=compartment,
            auth_mode=auth,
            config_file=env.get("OCI_CONFIG_FILE") or None,
            profile=env.get("OCI_PROFILE", "DEFAULT") or "DEFAULT",
            listen_host=host,
            listen_port=port,
            default_cloud=env.get("BRIDGE_DEFAULT_CLOUD") or None,
            default_service=env.get("BRIDGE_DEFAULT_SERVICE") or None,
        )

    def resource_defaults(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        if self.default_cloud:
            out["cloud.provider"] = self.default_cloud
        if self.default_service:
            out["service.name"] = self.default_service
        return out


class _BodyTooLarge(Exception):
    pass


def _inflate(body: bytes) -> bytes:
    """gzip-decode with the same size cap as the raw body (guards against bombs)."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(body, MAX_BODY_BYTES + 1)
    if d.unconsumed_tail or len(out) > MAX_BODY_BYTES:
        raise _BodyTooLarge()
    out += d.flush()
    if len(out) > MAX_BODY_BYTES:
        raise _BodyTooLarge()
    return out


def render_prometheus(stats: BridgeStats) -> str:
    snap = stats.snapshot()
    lines = [
        "# HELP bridge_dropped_points_total Points dropped or trimmed by the bridge, by reason.",
        "# TYPE bridge_dropped_points_total counter",
    ]
    for reason in DropReason:
        n = snap["dropped_total"].get(reason.value, 0)
        lines.append(f'bridge_dropped_points_total{{reason="{reason.value}"}} {n}')
    for name, help_text, key in (
        ("bridge_posted_streams_total", "Metric streams accepted by OCI Monitoring.", "posted_total"),
        ("bridge_failed_streams_total", "Metric streams rejected or failed after retries.", "failed_total"),
        ("bridge_post_batches_total", "PostMetricData calls completed.", "batches_total"),
    ):
        lines += [f"# HELP {name} {help_text}", f"# TYPE {name} counter", f"{name} {snap[key]}"]
    return "\n".join(lines) + "\n"


class _Handler(BaseHTTPRequestHandler):
    server_version = "metrics-bridge"
    sys_version = ""

    # -- routing ---------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send(HTTPStatus.OK, b"ok\n", "text/plain; charset=utf-8")
        elif path == "/metrics":
            with self.server.lock:
                text = render_prometheus(self.server.stats)
            self._send(HTTPStatus.OK, text.encode(), "text/plain; version=0.0.4; charset=utf-8")
        else:
            self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != METRICS_PATH:
            self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")
            return
        ctype = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if ctype != PROTOBUF_CONTENT_TYPE:
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                        f"unsupported Content-Type; send {PROTOBUF_CONTENT_TYPE} (JSON is not supported yet)")
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._error(HTTPStatus.LENGTH_REQUIRED, "Content-Length header required")
            return
        if length < 0:
            self._error(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
            return
        if length > MAX_BODY_BYTES:
            self.close_connection = True  # body is not read
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"body exceeds {MAX_BODY_BYTES} bytes")
            return
        body = self.rfile.read(length)
        try:
            payload = self._decode_body(body)
        except _BodyTooLarge:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"decoded body exceeds {MAX_BODY_BYTES} bytes")
            return
        except _UnsupportedEncoding as exc:
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, str(exc))
            return
        except Exception:  # noqa: BLE001 - corrupt gzip; never echo the body
            self._error(HTTPStatus.BAD_REQUEST, "malformed request body")
            return
        self._ingest(payload)

    # -- pipeline --------------------------------------------------------
    def _decode_body(self, body: bytes) -> bytes:
        encoding = self.headers.get("Content-Encoding", "").strip().lower()
        if encoding in ("", "identity"):
            if len(body) > MAX_BODY_BYTES:
                raise _BodyTooLarge()
            return body
        if encoding == "gzip":
            return _inflate(body)
        raise _UnsupportedEncoding(f"unsupported Content-Encoding: {encoding}")

    def _ingest(self, payload: bytes) -> None:
        server = self.server
        with server.lock:
            stats = server.stats
            dropped_before = _rejected_points(stats)
            failed_before = stats.failed_total
            try:
                points = server.decoder.decode(payload)
            except Exception as exc:  # noqa: BLE001 - log the type only
                log.warning("OTLP decode failed: %s", type(exc).__name__)
                self._error(HTTPStatus.BAD_REQUEST, "malformed OTLP metrics payload")
                return
            if points:
                try:
                    server.poster.post(points)
                except Exception as exc:  # noqa: BLE001
                    log.error("poster failed: %s", type(exc).__name__)
                    self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "metrics forwarding failed")
                    return
            rejected = _rejected_points(stats) - dropped_before
            failed = stats.failed_total - failed_before
        response = ExportMetricsServiceResponse()
        if rejected or failed:
            response.partial_success.rejected_data_points = rejected
            if failed:
                response.partial_success.error_message = f"{failed} metric streams failed to post"
        self._send(HTTPStatus.OK, response.SerializeToString(), PROTOBUF_CONTENT_TYPE)

    # -- helpers ---------------------------------------------------------
    def _error(self, status: HTTPStatus, message: str) -> None:
        self._send(status, (message + "\n").encode(), "text/plain; charset=utf-8")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # request line only; no bodies
        log.debug("%s %s", self.command, self.path.split("?", 1)[0])


class _UnsupportedEncoding(Exception):
    pass


def _rejected_points(stats: BridgeStats) -> int:
    return sum(n for reason, n in stats.dropped_total.items() if reason not in _NOT_COUNTED_AS_REJECTED)


class MetricsBridgeServer(ThreadingHTTPServer):
    """Threaded HTTP server; decoder state and posting are serialized by `lock`."""

    daemon_threads = True

    def __init__(self, address: Tuple[str, int], decoder: OtlpDecoder, poster) -> None:
        self.decoder = decoder
        self.stats: BridgeStats = decoder.stats
        self.poster = poster
        self.lock = threading.Lock()
        super().__init__(address, _Handler)


def build_server(config: BridgeConfig, poster=None,
                 listen: Optional[Tuple[str, int]] = None) -> MetricsBridgeServer:
    """Wire decoder, poster and HTTP server. `poster` is injectable for tests."""
    stats = BridgeStats()
    decoder = OtlpDecoder(stats=stats, defaults=config.resource_defaults())
    if poster is None:
        client = build_monitoring_client(config.region, config.auth_mode,
                                         config_file=config.config_file, profile=config.profile)
        poster = MonitoringPoster(client, config.compartment_id, stats=stats)
    host, port = listen or (config.listen_host, config.listen_port)
    return MetricsBridgeServer((host, port), decoder, poster)
