"""Vault-backed OIDC assertion files for the explicit cloud SDK identities.

Only workload OCI principals are admitted. Secrets stay in a private temporary
directory, are replaced atomically on refresh, and are deleted on shutdown.
Vault holds an externally issued, rotating JWT; it does not mint cloud tokens.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import os
import re
import signal
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Mapping


TOKEN_KEYS = {"aws": "AWS_WEB_IDENTITY_TOKEN_FILE", "azure": "AZURE_FEDERATED_TOKEN_FILE", "gcp": "GCP_WIF_TOKEN_FILE"}
MAX_TOKEN_BYTES = 64 * 1024
REFRESH_SECONDS = 60
MIN_VALIDITY_SECONDS = REFRESH_SECONDS + 30


class FederationError(ValueError):
    """Safe, fixed error code; never includes an SDK exception or secret."""


def _jwt(content: str) -> bytes:
    if not isinstance(content, str) or len(content) > MAX_TOKEN_BYTES * 2:
        raise FederationError("federation_token_unavailable")
    try:
        token = base64.b64decode(content, validate=True)
        if not token or len(token) > MAX_TOKEN_BYTES:
            raise ValueError
        parts = token.decode("ascii").split(".")
        if len(parts) != 3 or any(not re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in parts):
            raise ValueError
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        exp = claims.get("exp")
        if type(exp) not in (int, float) or not math.isfinite(exp) or exp <= time.time() + MIN_VALIDITY_SECONDS:
            raise ValueError
        # Claims are parsed only to reject expired placeholders. The destination
        # cloud verifies signature, audience, issuer and subject during exchange.
        return token
    except (ValueError, TypeError, AttributeError, UnicodeError, binascii.Error, RecursionError, OverflowError):
        raise FederationError("federation_token_unavailable") from None


class VaultToken:
    def __init__(self, client, secret_id: str, path: Path):
        self.client, self.secret_id, self.path = client, secret_id, path
        self.lock = threading.Lock()
        self.closed = False

    def refresh(self) -> None:
        try:
            from oci.retry import NoneRetryStrategy

            response = self.client.get_secret_bundle(secret_id=self.secret_id, stage="CURRENT",
                                                    retry_strategy=NoneRetryStrategy())
            bundle = response.data.secret_bundle_content
            if bundle.content_type != "BASE64":
                raise ValueError
            token = _jwt(bundle.content)
        except Exception:
            raise FederationError("federation_token_unavailable") from None
        with self.lock:
            if self.closed:
                return
            fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".refresh-")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(token)
                os.replace(temporary, self.path)
            finally:
                Path(temporary).unlink(missing_ok=True)

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self.path.unlink(missing_ok=True)

    def refresh_safely(self) -> bool:
        try:
            self.refresh()
            return True
        except Exception:
            with self.lock:
                self.path.unlink(missing_ok=True)
            logging.getLogger(__name__).warning("federation_token_refresh_unavailable")
            return False

    def run(self, stop: threading.Event) -> None:
        while not stop.wait(REFRESH_SECONDS):
            self.refresh_safely()


@contextmanager
def termination_cleanup():
    """Unwind secret scopes on managed SIGTERM shutdown (SIGKILL cannot unwind)."""
    if threading.current_thread() is not threading.main_thread():
        raise FederationError("federation_requires_main_thread")

    def terminate(signum, frame):
        raise SystemExit(128 + signum)

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


@contextmanager
def federation_environment(cloud: str, env: Mapping[str, str], *, client=None):
    """Resolve an optional Vault OCID without altering the process environment."""
    resolved = dict(env)
    secret_id = resolved.get("MC_FEDERATION_SECRET_OCID", "").strip()
    if not secret_id:
        yield resolved
        return
    key = TOKEN_KEYS.get(cloud)
    mode = resolved.get("OCI_AUTH_MODE", "resource_principal").strip()
    if (key is None or resolved.get(key) or mode not in {"resource_principal", "instance_principal"}
            or not secret_id.startswith("ocid1.vaultsecret.")):
        raise FederationError("federation_identity_configuration_invalid")
    if cloud == "aws" and resolved.get("AWS_AUTH_MODE", "web_identity") != "web_identity":
        raise FederationError("federation_identity_configuration_invalid")
    try:
        if client is None:
            import oci
            from .oci_auth import build_oci_client

            client = build_oci_client(oci.secrets.SecretsClient, auth_mode=mode,
                                      region=resolved.get("OCI_REGION") or None, timeout=(10, 30))
    except Exception:
        raise FederationError("federation_token_unavailable") from None
    with termination_cleanup(), tempfile.TemporaryDirectory(prefix="mc-federation-") as directory:
        token = VaultToken(client, secret_id, Path(directory) / "token")
        token.refresh()
        resolved[key] = str(token.path)
        stop = threading.Event()
        worker = threading.Thread(target=token.run, args=(stop,), name="vault-federation-refresh", daemon=True)
        worker.start()
        try:
            yield resolved
        finally:
            stop.set()
            # Remove assertions before waiting; a late SDK result cannot recreate
            # the file after close, even once the temporary directory is gone.
            token.close()
            worker.join(timeout=1)
