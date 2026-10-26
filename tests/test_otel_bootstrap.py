"""Offline tests for the per-cloud Vault-aware OTel launcher."""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from collectors.common.federation import FederationError
from collectors.otel import bootstrap

SERVICE_ACCOUNT = "metrics-reader@" + "example-project" + ".iam.gserviceaccount.com"


@contextmanager
def _resolved(values: dict[str, str]):
    yield dict(values)


def test_aws_uses_web_identity_file_without_exposing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}
    monkeypatch.setattr(bootstrap, "federation_environment", lambda cloud, env: _resolved({
        **env, "AWS_WEB_IDENTITY_TOKEN_FILE": "/private/token"
    }))
    _capture_child(monkeypatch, captured, 7)
    assert bootstrap.run_collector("aws", "/cfg.yaml", {"AWS_ROLE_ARN": "arn:aws:iam::1:role/x"}) == 7
    assert captured["command"] == ["/usr/local/bin/otelcol-contrib", "--config", "/cfg.yaml"]
    assert captured["env"]["AWS_WEB_IDENTITY_TOKEN_FILE"] == "/private/token"


def test_azure_preserves_launcher_token_file(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}
    monkeypatch.setattr(bootstrap, "federation_environment", lambda cloud, env: _resolved({
        **env, "AZURE_FEDERATED_TOKEN_FILE": "/private/token"
    }))
    _capture_child(monkeypatch, captured, 0)
    assert bootstrap.run_collector("azure", "/cfg.yaml", {}) == 0
    assert captured["env"]["AZURE_FEDERATED_TOKEN_FILE"] == "/private/token"


def test_gcp_writes_external_account_config_mode_600(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("assertion", encoding="utf-8")
    captured = {}
    monkeypatch.setattr(bootstrap, "federation_environment", lambda cloud, env: _resolved({
        **env,
        "GCP_WIF_TOKEN_FILE": str(token),
        "GCP_METRICS_SERVICE_ACCOUNT_EMAIL": SERVICE_ACCOUNT,
        "GCP_WIF_PROVIDER_RESOURCE": "projects/123/locations/global/workloadIdentityPools/pool/providers/provider",
    }))
    _capture_child(monkeypatch, captured, 0)
    assert bootstrap.run_collector("gcp", "/cfg.yaml", {}) == 0
    config_path = Path(captured["env"]["GOOGLE_APPLICATION_CREDENTIALS"])
    assert not config_path.exists()
    assert captured["config_mode"] == 0o600
    assert captured["config"] == {
        "type": "external_account",
        "audience": "//iam.googleapis.com/projects/123/locations/global/workloadIdentityPools/pool/providers/provider",
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        "token_url": "https://sts.googleapis.com/v1/token",
        "service_account_impersonation_url": (
            "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
            f"{SERVICE_ACCOUNT}:generateAccessToken"
        ),
        "credential_source": {"file": str(token), "format": {"type": "text"}},
    }


@pytest.mark.parametrize("provider", ["", "projects/nope", "projects/1/locations/us/workloadIdentityPools/p/providers/x"])
def test_gcp_rejects_malformed_provider(provider: str, tmp_path: Path) -> None:
    with pytest.raises(FederationError, match="federation_identity_configuration_invalid"):
        bootstrap._gcp_credential_config(str(tmp_path / "token"), provider, SERVICE_ACCOUNT, str(tmp_path))


def test_main_returns_safe_error_without_running_child(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(bootstrap, "run_collector", lambda *args: (_ for _ in ()).throw(FederationError("federation_token_unavailable")))
    assert bootstrap.main(["--cloud", "aws", "--config", "/cfg.yaml"]) == 2
    assert capsys.readouterr().err == "federation_token_unavailable\n"


def _capture_child(monkeypatch, captured, status):
    class Child:
        def __init__(self, command, **kwargs):
            captured.update(command=command, **kwargs)
            credential_path = kwargs["env"].get("GOOGLE_APPLICATION_CREDENTIALS")
            if credential_path:
                path = Path(credential_path)
                captured["config_mode"] = path.stat().st_mode & 0o777
                captured["config"] = json.loads(path.read_text())

        def wait(self, timeout=None):
            return status

    monkeypatch.setattr(bootstrap.subprocess, "Popen", Child)


def test_shutdown_stops_child_before_identity_cleanup(monkeypatch):
    events = []

    @contextmanager
    def identity(cloud, env):
        try:
            yield {}
        finally:
            events.append("identity_removed")

    class Child:
        calls = 0

        def __init__(self, *args, **kwargs):
            pass

        def wait(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise SystemExit(143)
            assert timeout == 5
            events.append("child_reaped")
            return -15

        def terminate(self):
            events.append("child_terminated")

    monkeypatch.setattr(bootstrap, "collector_environment", identity)
    monkeypatch.setattr(bootstrap.subprocess, "Popen", Child)
    with pytest.raises(SystemExit) as exc:
        bootstrap.run_collector("aws", "/cfg", {})
    assert exc.value.code == 143
    assert events == ["child_terminated", "child_reaped", "identity_removed"]


def test_gcp_projected_token_directory_untouched(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("synthetic-assertion")
    with bootstrap.collector_environment("gcp", {
        "GCP_WIF_TOKEN_FILE": str(token),
        "GCP_METRICS_SERVICE_ACCOUNT_EMAIL": SERVICE_ACCOUNT,
        "GCP_WIF_PROVIDER_RESOURCE": "projects/123/locations/global/workloadIdentityPools/pool/providers/provider",
    }) as resolved:
        config = Path(resolved["GOOGLE_APPLICATION_CREDENTIALS"])
        assert config.parent != token.parent
        assert config.parent.stat().st_mode & 0o777 == 0o700
    assert list(tmp_path.iterdir()) == [token]
    assert not config.exists()


@pytest.mark.parametrize("account", ["", "reader", "reader@example.invalid", "reader@example.invalid/path"])
def test_gcp_invalid_reader_refuses_child_start(monkeypatch, tmp_path, account):
    captured = {}
    _capture_child(monkeypatch, captured, 0)
    monkeypatch.setattr(bootstrap, "federation_environment", lambda cloud, env: _resolved(env))
    with pytest.raises(FederationError, match="federation_identity_configuration_invalid"):
        bootstrap.run_collector("gcp", "/cfg.yaml", {
            "GCP_WIF_TOKEN_FILE": str(tmp_path / "token"),
            "GCP_WIF_PROVIDER_RESOURCE": "projects/123/locations/global/workloadIdentityPools/pool/providers/provider",
            "GCP_METRICS_SERVICE_ACCOUNT_EMAIL": account,
        })
    assert captured == {}


@pytest.mark.parametrize("extra", [["--otel-bin", "/other"], ["--set=x"], ["--", "extra"]])
def test_main_rejects_launcher_overrides(monkeypatch, extra):
    captured = {}
    _capture_child(monkeypatch, captured, 0)
    with pytest.raises(SystemExit) as exc:
        bootstrap.main(["--cloud", "aws", "--config", "/cfg.yaml", *extra])
    assert exc.value.code == 2
    assert captured == {}
