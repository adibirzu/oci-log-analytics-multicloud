import base64
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from collectors.common.federation import TOKEN_KEYS, FederationError, VaultToken, federation_environment


def jwt(exp=None):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp or time.time() + 3600}).encode()).decode().rstrip("=")
    return "eyJhbGciOiJSUzI1NiJ9." + payload + ".synthetic-signature"


class Client:
    def __init__(self, token):
        self.token = token
        self.calls = []

    def get_secret_bundle(self, **kwargs):
        self.calls.append(kwargs)
        content = SimpleNamespace(content_type="BASE64", content=base64.b64encode(self.token.encode()).decode())
        return SimpleNamespace(data=SimpleNamespace(secret_bundle_content=content))


def env():
    return {"MC_FEDERATION_SECRET_OCID": "ocid" + "1.vaultsecret.oc1..fixture", "OCI_AUTH_MODE": "resource_principal"}


@pytest.mark.parametrize("cloud,key", [("aws", "AWS_WEB_IDENTITY_TOKEN_FILE"),
                                        ("azure", "AZURE_FEDERATED_TOKEN_FILE"), ("gcp", "GCP_WIF_TOKEN_FILE")])
def test_secure_file_and_cleanup(cloud, key):
    client = Client(jwt())
    with federation_environment(cloud, env(), client=client) as resolved:
        path = Path(resolved[key])
        assert path.read_text() == client.token
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert key not in env()
        assert len(client.calls) == 1
        assert client.calls[0]["secret_id"] == env()["MC_FEDERATION_SECRET_OCID"]
        assert client.calls[0]["stage"] == "CURRENT"
        assert type(client.calls[0]["retry_strategy"]).__name__ == "NoneRetryStrategy"
    assert not path.exists() and not path.parent.exists()


@pytest.mark.parametrize("token", ["PLACEHOLDER", "", "not-a-jwt", jwt(time.time() - 60), jwt(time.time() + 59)])
def test_invalid_bundle_refused(token):
    with pytest.raises(FederationError, match="federation_token_unavailable"):
        with federation_environment("aws", env(), client=Client(token)):
            pytest.fail("invalid token admitted")


@pytest.mark.parametrize("mode", ["auto", "api_key", "typo"])
def test_no_operator_fallback(mode):
    with pytest.raises(FederationError):
        with federation_environment("aws", {**env(), "OCI_AUTH_MODE": mode}, client=Client(jwt())):
            pytest.fail("operator auth admitted")


def test_existing_projected_token_unchanged():
    original = {"AWS_WEB_IDENTITY_TOKEN_FILE": "/projected/token"}
    with federation_environment("aws", original) as resolved:
        assert resolved == original


@pytest.mark.parametrize("cloud,key", list(TOKEN_KEYS.items()))
def test_all_projected_token_modes_preserved(cloud, key):
    original = {key: "/projected/token"}
    with federation_environment(cloud, original) as resolved:
        assert resolved == original


def test_conflicting_projected_token_refused():
    with pytest.raises(FederationError):
        with federation_environment("aws", {**env(), "AWS_WEB_IDENTITY_TOKEN_FILE": "/projected/token"}, client=Client(jwt())):
            pytest.fail("ambiguous identity admitted")


def test_rotation_and_failure_removes_token(tmp_path):
    client = Client(jwt())
    token = VaultToken(client, env()["MC_FEDERATION_SECRET_OCID"], tmp_path / "token")
    token.refresh()
    old = token.path.read_text()
    client.token = jwt(time.time() + 7200)
    token.refresh()
    assert token.path.read_text() != old
    client.token = "PLACEHOLDER"
    assert token.refresh_safely() is False
    assert not token.path.exists()
    client.token = jwt()
    assert token.refresh_safely() is True


def test_sdk_error_does_not_leak(caplog):
    class Broken:
        def get_secret_bundle(self, **kwargs):
            raise RuntimeError("synthetic-private-token")
    with pytest.raises(FederationError) as exc:
        with federation_environment("aws", env(), client=Broken()):
            pass
    assert "synthetic-private-token" not in str(exc.value) + caplog.text


def test_refresh_scheduler(tmp_path):
    client = Client(jwt())
    token = VaultToken(client, env()["MC_FEDERATION_SECRET_OCID"], tmp_path / "token")

    class Stop:
        calls = 0

        def wait(self, interval):
            assert interval == 60
            self.calls += 1
            return self.calls > 1

    token.run(Stop())
    assert len(client.calls) == 1
    assert token.path.read_text() == client.token


def test_close_prevents_inflight_refresh_recreating_token(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Blocked(Client):
        def get_secret_bundle(self, **kwargs):
            entered.set()
            assert release.wait(5)
            return super().get_secret_bundle(**kwargs)

    token = VaultToken(Blocked(jwt()), env()["MC_FEDERATION_SECRET_OCID"], tmp_path / "token")
    token.path.write_text("synthetic-old-assertion")
    worker = threading.Thread(target=token.refresh)
    worker.start()
    try:
        assert entered.wait(5)
        token.close()
        assert not token.path.exists()
        release.set()
        worker.join(timeout=5)
        assert not worker.is_alive() and not token.path.exists()
    finally:
        release.set()
        worker.join(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="POSIX managed shutdown")
def test_sigterm_removes_materialized_token(tmp_path):
    ready = tmp_path / "ready"
    code = '''
import sys, time
from pathlib import Path
from tests.test_federation_vault import Client, jwt, env
from collectors.common.federation import federation_environment
with federation_environment("aws", env(), client=Client(jwt())) as resolved:
    Path(sys.argv[1]).write_text(resolved["AWS_WEB_IDENTITY_TOKEN_FILE"])
    while True:
        time.sleep(1)
'''
    # Tests is not a package in all installs; make its helper module explicit.
    code = code.replace("from tests.test_federation_vault", "from test_federation_vault")
    child_env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent) + os.pathsep + str(Path(__file__).parents[1])}
    child = subprocess.Popen([sys.executable, "-c", code, str(ready)], env=child_env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "child did not materialize a token"
        path = Path(ready.read_text())
        assert path.exists()
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=10) == 143
        assert not path.exists() and not path.parent.exists()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


def test_sigterm_during_blocked_refresh(tmp_path):
    ready = tmp_path / "ready"
    code = '''
import sys, time, threading
from pathlib import Path
from test_federation_vault import Client, jwt, env
import collectors.common.federation as federation
federation.REFRESH_SECONDS = 0.01
class Blocked(Client):
    calls_count = 0
    def get_secret_bundle(self, **kwargs):
        self.calls_count += 1
        if self.calls_count > 1:
            Path(sys.argv[1] + ".blocked").write_text("ready")
            threading.Event().wait(60)
        return super().get_secret_bundle(**kwargs)
with federation.federation_environment("aws", env(), client=Blocked(jwt())) as resolved:
    Path(sys.argv[1]).write_text(resolved["AWS_WEB_IDENTITY_TOKEN_FILE"])
    while True:
        time.sleep(1)
'''
    child_env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent) + os.pathsep + str(Path(__file__).parents[1])}
    child = subprocess.Popen([sys.executable, "-c", code, str(ready)], env=child_env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 15
        while not ready.with_suffix(".blocked").exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.with_suffix(".blocked").exists()
        path = Path(ready.read_text())
        started = time.monotonic()
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=5) == 143
        assert time.monotonic() - started < 5
        assert not path.exists() and not path.parent.exists()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


@pytest.mark.parametrize("cloud,key", list(TOKEN_KEYS.items()))
def test_entrypoint_passes_vault_environment_to_adapter(monkeypatch, cloud, key):
    from collectors.logs import __main__ as entry
    from contextlib import contextmanager
    observed = []

    @contextmanager
    def resolved(source, environment):
        assert source == cloud
        yield {key: "/private/runtime/token"}

    def run(args, environment, parser):
        observed.append(environment[key])
        return 0

    monkeypatch.setattr("collectors.common.federation.federation_environment", resolved)
    monkeypatch.setattr(entry, "run_source", run)
    assert entry.main(["--source", cloud]) == 0
    assert observed == ["/private/runtime/token"]
