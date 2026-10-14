"""Offline checks for collectors/otel/apm-gateway.yaml (MC-16)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

GATEWAY = Path(__file__).resolve().parents[1] / "collectors" / "otel" / "apm-gateway.yaml"
ENV_ONLY = re.compile(r"\$\{env:[A-Za-z_][A-Za-z0-9_]*(:-[^}]*)?\}")
SECRET_KEY = re.compile(r"datakey|secret|token|password|api[_-]?key|access[_-]?key", re.IGNORECASE)


def _config() -> dict:
    return yaml.safe_load(GATEWAY.read_text(encoding="utf-8"))


def _strings(node):
    """Yield every string scalar in the tree, regardless of its key."""
    if isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)
    elif isinstance(node, str):
        yield node


def _walk_pairs(node, parent=None):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _walk_pairs(value, key)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_pairs(item, parent)


def test_parses_with_required_sections() -> None:
    config = _config()
    assert {"receivers", "processors", "exporters", "service"} <= set(config)
    assert config["receivers"]["otlp"]["protocols"]["grpc"]["endpoint"].endswith(":4317")
    assert config["receivers"]["otlp"]["protocols"]["http"]["endpoint"].endswith(":4318")


def test_every_pipeline_references_defined_components() -> None:
    config = _config()
    defined = {
        "receivers": set(config["receivers"]),
        "processors": set(config["processors"]),
        "exporters": set(config["exporters"]),
    }
    pipelines = config["service"]["pipelines"]
    assert pipelines
    for name, pipeline in pipelines.items():
        for kind, ids in (("receivers", pipeline["receivers"]),
                          ("processors", pipeline["processors"]),
                          ("exporters", pipeline["exporters"])):
            missing = set(ids) - defined[kind]
            assert not missing, f"{name} references undefined {kind}: {missing}"


def test_processors_present_in_expected_order() -> None:
    traces = _config()["service"]["pipelines"]["traces"]["processors"]
    assert traces == ["memory_limiter", "filter/health", "probabilistic_sampler", "batch"]


def test_health_check_filter_covers_probe_paths() -> None:
    text = GATEWAY.read_text(encoding="utf-8")
    for path in ("/health", "/healthz", "/ready", "/live"):
        assert f'"{path}"' in text
    assert "http.route" in text and "url.path" in text


def test_sampler_default_is_env_templated() -> None:
    value = _config()["processors"]["probabilistic_sampler"]["sampling_percentage"]
    assert value == "${env:APM_SAMPLING_PERCENT:-10}"


def test_apm_endpoints_and_auth_header() -> None:
    exporter = _config()["exporters"]["otlp_http/apm"]
    assert exporter["traces_endpoint"] == (
        "${env:OCI_APM_ENDPOINT}/20200101/opentelemetry/private/v1/traces"
    )
    assert exporter["metrics_endpoint"] == "${env:OCI_APM_ENDPOINT}/20200101/opentelemetry/v1/metrics"
    assert exporter["headers"]["Authorization"] == "dataKey ${env:OCI_APM_PRIVATE_DATAKEY}"


def test_no_literal_secrets() -> None:
    for key, value in _walk_pairs(_config()):
        if isinstance(value, str) and key is not None and SECRET_KEY.search(str(key)):
            assert ENV_ONLY.fullmatch(value) or value.startswith("dataKey ${env:"), (key, value)
    for text in _strings(_config()):
        # Any ${...} must be an env reference; no other interpolation sources.
        for match in re.findall(r"\$\{[^}]*\}", text):
            assert match.startswith("${env:"), match
