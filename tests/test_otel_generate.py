"""Offline tests for the metrics-pull generator (MC-14). No cloud calls."""

from __future__ import annotations

import copy
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from collectors.otel.generate import ConfigError, main, render, validate_config

ROOT = Path(__file__).resolve().parents[1]
OTEL_DIR = ROOT / "collectors" / "otel"
FIXTURES = ROOT / "tests" / "fixtures" / "otel"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _base() -> dict:
    return copy.deepcopy(_load(FIXTURES / "metrics-pull.all-clouds.yaml"))


def test_example_allowlist_validates() -> None:
    validate_config(_load(OTEL_DIR / "metrics-pull.example.yaml"))


@pytest.mark.parametrize("interval", [0, 59, 120, 299])
def test_schema_rejects_interval_below_floor(interval: int) -> None:
    data = _base()
    data["clouds"]["aws"]["interval"] = interval
    with pytest.raises(ConfigError, match="interval|minimum"):
        validate_config(data)


def test_schema_rejects_interval_below_60_with_location() -> None:
    data = _base()
    data["clouds"]["gcp"]["interval"] = 59
    with pytest.raises(ConfigError, match="clouds/gcp/interval"):
        validate_config(data)


@pytest.mark.parametrize("cloud", ["aws", "azure", "gcp"])
def test_schema_rejects_empty_allowlist(cloud: str) -> None:
    data = _base()
    data["clouds"][cloud]["metrics"] = []
    with pytest.raises(ConfigError, match="metrics"):
        validate_config(data)


def test_schema_rejects_missing_allowlist() -> None:
    data = _base()
    del data["clouds"]["azure"]["metrics"]
    with pytest.raises(ConfigError, match="metrics"):
        validate_config(data)


def test_schema_rejects_unknown_identity_mode() -> None:
    data = _base()
    data["clouds"]["aws"]["identity"] = "access_keys"
    with pytest.raises(ConfigError, match="identity"):
        validate_config(data)


def test_rejects_literal_secret_in_input() -> None:
    data = _base()
    data["clouds"]["aws"]["access_key"] = "not-" + "a-reference"
    with pytest.raises(ConfigError, match="secret-like"):
        validate_config(data)


@pytest.mark.parametrize("key", ["client_secret", "password", "token", "secret"])
def test_rejects_literal_secret_under_any_cloud(key: str) -> None:
    data = _base()
    data["clouds"]["gcp"][key] = "literal-" + "value"
    with pytest.raises(ConfigError, match="secret-like"):
        validate_config(data)


def test_accepts_env_reference_for_secret_like_key() -> None:
    data = _base()
    data["clouds"]["gcp"]["token"] = "${env:COLLECTOR_TOKEN}"
    validate_config(data)


def test_refuses_when_no_cloud_enabled() -> None:
    data = _base()
    for cfg in data["clouds"].values():
        cfg["enabled"] = False
    with pytest.raises(ConfigError, match="at least one"):
        validate_config(data)


def test_golden_all_clouds() -> None:
    data = _load(FIXTURES / "metrics-pull.all-clouds.yaml")
    expected = (FIXTURES / "golden-all-clouds.yaml").read_text(encoding="utf-8")
    assert render(data) == expected


def test_golden_single_cloud() -> None:
    data = _load(FIXTURES / "metrics-pull.aws-only.yaml")
    expected = (FIXTURES / "golden-aws-only.yaml").read_text(encoding="utf-8")
    assert render(data) == expected


def test_single_cloud_has_no_other_components() -> None:
    config = yaml.safe_load(render(_load(FIXTURES / "metrics-pull.aws-only.yaml")))
    assert set(config["receivers"]) == {"awscloudwatch/eu-central-1"}
    assert "azuremonitor" not in config["receivers"]
    assert "googlecloudmonitoring" not in config["receivers"]
    assert "otlp_http/apm" not in config["exporters"]  # default sink is monitoring


def test_all_clouds_pipelines_and_sink() -> None:
    config = yaml.safe_load(render(_load(FIXTURES / "metrics-pull.all-clouds.yaml")))
    assert set(config["service"]["pipelines"]) == {"metrics/aws", "metrics/azure", "metrics/gcp"}
    for pipeline in config["service"]["pipelines"].values():
        assert pipeline["exporters"] == ["otlp_http/bridge", "otlp_http/apm"]  # sink = both
        assert pipeline["processors"] == ["memory_limiter", pipeline["processors"][1], "batch"]


def test_apm_sink_drops_bridge_from_pipelines() -> None:
    data = _base()
    data["metrics_sink"] = "apm"
    config = yaml.safe_load(render(data))
    for pipeline in config["service"]["pipelines"].values():
        assert pipeline["exporters"] == ["otlp_http/apm"]


def test_no_literal_secrets_in_output() -> None:
    text = render(_load(FIXTURES / "metrics-pull.all-clouds.yaml"))
    config = yaml.safe_load(text)
    assert config["exporters"]["otlp_http/apm"]["headers"]["Authorization"] == (
        "dataKey ${env:OCI_APM_PRIVATE_DATAKEY}"
    )
    assert "AWS_SECRET" not in text and "client_secret" not in text
    assert "aws_access_key" not in text.lower()


def test_no_static_keys_in_receivers() -> None:
    text = render(_load(FIXTURES / "metrics-pull.all-clouds.yaml"))
    for forbidden in ("access_key", "secret_key", "client_secret", "credentials_file", "password"):
        assert forbidden not in text


def test_cli_writes_output_and_rejects_bad_input(tmp_path: Path) -> None:
    out = tmp_path / "collector.yaml"
    assert main(["--input", str(FIXTURES / "metrics-pull.aws-only.yaml"), "--output", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == (FIXTURES / "golden-aws-only.yaml").read_text(encoding="utf-8")
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\nclouds:\n  aws:\n    enabled: true\n    interval: 30\n", encoding="utf-8")
    assert main(["--input", str(bad)]) == 2


# --- Real-binary validation (opt-in). Set OTELCOL_BIN to an otelcol-contrib binary. ---

VALIDATION_ENV = {
    "AWS_REGION": "us-east-1",
    "AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000000",
    "AZURE_TENANT_ID": "00000000-0000-0000-0000-000000000000",
    "AZURE_CLIENT_ID": "00000000-0000-0000-0000-000000000000",
    "GCP_PROJECT_ID": "example-project",
    "METRICS_BRIDGE_ENDPOINT": "http://127.0.0.1:4319",
    "OCI_APM_ENDPOINT": "https://example.invalid",
    "OCI_APM_PRIVATE_DATAKEY": "dummy",
    "APM_SAMPLING_PERCENT": "10",
}
OTELCOL_BIN = os.environ.get("OTELCOL_BIN")
requires_otelcol = pytest.mark.skipif(not OTELCOL_BIN, reason="set OTELCOL_BIN to validate with otelcol-contrib")


@requires_otelcol
@pytest.mark.parametrize(
    "golden",
    [FIXTURES / "golden-all-clouds.yaml", FIXTURES / "golden-aws-only.yaml", OTEL_DIR / "apm-gateway.yaml"],
    ids=lambda p: p.name,
)
def test_real_otelcol_validates_config(golden: Path) -> None:
    env = {**VALIDATION_ENV, **os.environ}
    result = subprocess.run(
        [OTELCOL_BIN, "validate", "--config", str(golden)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_azure_metrics_use_namespace_metric_aggregation_shape() -> None:
    config = yaml.safe_load((FIXTURES / "golden-all-clouds.yaml").read_text(encoding="utf-8"))
    metrics = config["receivers"]["azuremonitor"]["metrics"]
    for namespace, by_metric in metrics.items():
        assert namespace == namespace.lower()
        assert "default" not in by_metric  # metric names map to aggregation lists
        assert all(isinstance(aggs, list) for aggs in by_metric.values())


def test_azure_workload_identity_uses_launcher_selected_file() -> None:
    data = _base()
    data["clouds"]["azure"]["identity"] = "workload_identity"
    config = yaml.safe_load(render(data))
    identity = config["extensions"]["azureauth"]["workload_identity"]
    assert identity["federated_token_file"] == "${env:AZURE_FEDERATED_TOKEN_FILE}"
    assert "azureauth" in config["service"]["extensions"]
