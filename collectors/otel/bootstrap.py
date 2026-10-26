"""Start one OTel Collector with an optional OCI Vault-backed cloud assertion.

The process owns a single cloud identity.  It never combines cloud assertions in
one environment.  When MC_FEDERATION_SECRET_OCID is unset, the regular cloud
SDK workload identity chain is left untouched.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from collectors.common.federation import FederationError, federation_environment, termination_cleanup

_GCP_PROVIDER = re.compile(
    r"^projects/[0-9]+/locations/global/workloadIdentityPools/[A-Za-z0-9_-]+/providers/[A-Za-z0-9_-]+$"
)


def _gcp_credential_config(token_file: str, provider: str, service_account: str, directory: str) -> str:
    """Write a mode-0600 external-account config in a private temporary directory."""
    if not _GCP_PROVIDER.fullmatch(provider) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]*@[a-z0-9][a-z0-9-]*\.iam\.gserviceaccount\.com", service_account
    ):
        raise FederationError("federation_identity_configuration_invalid")
    path = Path(directory) / "google-external-account.json"
    content = {
        "type": "external_account",
        "audience": f"//iam.googleapis.com/{provider}",
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        "token_url": "https://sts.googleapis.com/v1/token",
        "service_account_impersonation_url": (
            "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
            f"{service_account}:generateAccessToken"
        ),
        "credential_source": {"file": token_file, "format": {"type": "text"}},
    }
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(content, stream, separators=(",", ":"))
    return str(path)


@contextmanager
def collector_environment(cloud: str, env: Mapping[str, str]) -> Iterator[dict[str, str]]:
    """Map the common assertion file into the SDK-specific cloud environment."""
    with federation_environment(cloud, env) as resolved:
        if cloud == "gcp" and resolved.get("GCP_WIF_TOKEN_FILE"):
            provider = resolved.get("GCP_WIF_PROVIDER_RESOURCE", "").strip()
            with tempfile.TemporaryDirectory(prefix="mc-otel-gcp-") as directory:
                resolved["GOOGLE_APPLICATION_CREDENTIALS"] = _gcp_credential_config(
                    resolved["GCP_WIF_TOKEN_FILE"], provider,
                    resolved.get("GCP_METRICS_SERVICE_ACCOUNT_EMAIL", "").strip(), directory
                )
                yield resolved
        else:
            yield resolved


def run_collector(cloud: str, config: str, env: Mapping[str, str]) -> int:
    """Run the collector as a child and return its exit status without logging secrets."""
    with termination_cleanup(), collector_environment(cloud, env) as resolved:
        child = subprocess.Popen(["/usr/local/bin/otelcol-contrib", "--config", config], env=resolved)
        try:
            return int(child.wait())
        except BaseException:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud", choices=("aws", "azure", "gcp"), required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        return run_collector(args.cloud, args.config, os.environ)
    except (FederationError, OSError):
        print("federation_token_unavailable", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
