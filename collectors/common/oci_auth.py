"""Centralized OCI authentication resolution for service adapters."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Tuple

logger = logging.getLogger("mc.OCIAuth")

AuthConfig = dict[str, Any]
Signer = Any
ResolvedAuth = Tuple[AuthConfig, Signer | None]

SUPPORTED_AUTH_MODES = frozenset(
    {"auto", "api_key", "instance_principal", "resource_principal"}
)


def _external_probes_disabled() -> bool:
    """Return whether automatic external credential discovery is prohibited."""
    return os.getenv("MC_DISABLE_EXTERNAL_PROBES", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def get_auth_mode() -> str:
    """Return the normalized OCI auth mode from the environment."""
    mode = os.getenv("OCI_AUTH_MODE", "auto").strip().lower()
    if mode not in SUPPORTED_AUTH_MODES:
        logger.warning("Unsupported OCI_AUTH_MODE=%s; falling back to auto", mode)
        return "auto"
    return mode


def _config_file_path(config_file: str | None = None) -> str:
    raw = config_file or os.getenv("OCI_CONFIG_FILE", "~/.oci/config")
    return os.path.expanduser(raw)


def _config_profile(profile: str | None = None) -> str:
    return profile or os.getenv("OCI_CONFIG_PROFILE", os.getenv("OCI_PROFILE", "DEFAULT"))


def _api_key_auth(
    config_file: str | None = None,
    profile: str | None = None,
) -> ResolvedAuth:
    import oci

    config = oci.config.from_file(
        file_location=_config_file_path(config_file),
        profile_name=_config_profile(profile),
    )
    oci.config.validate_config(config)
    return config, None


def _instance_principal_auth() -> ResolvedAuth:
    import oci

    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    return {}, signer


def _resource_principal_auth() -> ResolvedAuth:
    import oci

    try:
        signer = oci.auth.signers.get_oke_workload_identity_resource_principal_signer()
    except Exception:
        signer = oci.auth.signers.get_resource_principals_signer()
    return {}, signer


def resolve_oci_auth(
    auth_mode: str | None = None,
    *,
    config_file: str | None = None,
    profile: str | None = None,
) -> ResolvedAuth:
    """Resolve OCI client config and signer for the requested auth mode."""
    mode = _normalize_auth_mode(auth_mode)

    if mode == "instance_principal":
        return _instance_principal_auth()
    if mode == "resource_principal":
        return _resource_principal_auth()
    if mode == "api_key":
        return _api_key_auth(config_file=config_file, profile=profile)

    if _external_probes_disabled():
        raise RuntimeError(
            "automatic OCI authentication is unavailable because external OCI probes are disabled"
        )

    # auto: prefer instance principal on OCI compute, otherwise config file.
    try:
        return _instance_principal_auth()
    except Exception as exc:
        logger.debug("Instance principal unavailable in auto mode: %s", exc)
        return _api_key_auth(config_file=config_file, profile=profile)


def _normalize_auth_mode(auth_mode: str | None) -> str:
    if isinstance(auth_mode, str):
        mode = auth_mode.strip().lower()
        if mode in SUPPORTED_AUTH_MODES:
            return mode
    return get_auth_mode()


def is_oci_auth_configured(
    auth_mode: str | None = None,
    *,
    config_file: str | None = None,
) -> bool:
    """Return whether OCI auth can be attempted for the current mode."""
    mode = _normalize_auth_mode(auth_mode)
    if mode in {"instance_principal", "resource_principal"}:
        return True
    if mode == "auto":
        return not _external_probes_disabled()
    return Path(_config_file_path(config_file)).exists()


def get_tenancy_id(
    auth_mode: str | None = None,
    *,
    config_file: str | None = None,
    profile: str | None = None,
) -> str:
    """Return the tenancy OCID for the active auth mode."""
    config, signer = resolve_oci_auth(
        auth_mode,
        config_file=config_file,
        profile=profile,
    )
    if signer is not None and getattr(signer, "tenancy_id", None):
        return str(signer.tenancy_id)
    return str(config.get("tenancy", ""))


def build_oci_client(
    client_cls: type,
    auth_mode: str | None = None,
    *,
    config_file: str | None = None,
    profile: str | None = None,
    region: str | None = None,
    timeout: Any | None = None,
) -> Any:
    """Instantiate an OCI SDK client using the resolved auth mode."""
    client_config, signer = resolve_oci_auth(
        auth_mode,
        config_file=config_file,
        profile=profile,
    )
    if region:
        client_config = dict(client_config)
        client_config["region"] = region
    client_kwargs = {}
    if timeout is not None:
        client_kwargs["timeout"] = timeout
    if signer is not None:
        client = client_cls(client_config, signer=signer, **client_kwargs)
    else:
        client = client_cls(client_config, **client_kwargs)
    if region and hasattr(client, "base_client"):
        client.base_client.set_region(region)
    return client


def build_oci_client_with_signer(
    client_cls: type,
    auth_mode: str | None = None,
    *,
    config_file: str | None = None,
    profile: str | None = None,
    region: str | None = None,
) -> tuple[Any, Signer | None]:
    """Instantiate an OCI SDK client and return the active signer when present."""
    client_config, signer = resolve_oci_auth(
        auth_mode,
        config_file=config_file,
        profile=profile,
    )
    if region:
        client_config = dict(client_config)
        client_config["region"] = region
    if signer is not None:
        client = client_cls(client_config, signer=signer)
        if region and hasattr(client, "base_client"):
            client.base_client.set_region(region)
        return client, signer
    client = client_cls(client_config)
    if region and hasattr(client, "base_client"):
        client.base_client.set_region(region)
    return client, None


def build_oci_api_signer(
    auth_mode: str | None = None,
    *,
    config_file: str | None = None,
    profile: str | None = None,
) -> tuple[Signer, AuthConfig]:
    """Return an OCI HTTP signer plus config metadata for direct REST calls."""
    import oci

    client_config, signer = resolve_oci_auth(
        auth_mode,
        config_file=config_file,
        profile=profile,
    )
    if signer is not None:
        region = os.getenv("OCI_REGION", "").strip()
        if not region:
            region = str(client_config.get("region", "") or getattr(signer, "region", "")).strip()
        if not region:
            raise ValueError(
                "OCI_REGION is required; set OCI_REGION or configure region in the active OCI auth profile"
            )
        return signer, {"region": region}
    api_signer = oci.Signer(
        tenancy=client_config["tenancy"],
        user=client_config["user"],
        fingerprint=client_config["fingerprint"],
        private_key_file_location=client_config.get("key_file"),
        pass_phrase=client_config.get("pass_phrase"),
    )
    return api_signer, client_config
