"""Pre-mutation identity checks against config and repo policy."""
from __future__ import annotations

from typing import Callable

from mcctl.config import CloudConfig

# oci_identity(profile, compartment_name) -> {"tenancy_name": str, "compartment_name": str | None}
OciIdentity = Callable[[str, str], dict]


class PreflightError(RuntimeError):
    pass


def check(cc: CloudConfig, oci_identity: OciIdentity) -> None:
    t = cc.oci
    # Config-driven guard: `protected_profiles: {<profile>: <allowed_compartment>}` (default empty).
    allowed = t.protected_profiles.get(t.profile)
    if allowed is not None and t.compartment_name != allowed:
        raise PreflightError(f"profile {t.profile} is read-only outside compartment {allowed}")
    ident = oci_identity(t.profile, t.compartment_name)
    if ident.get("tenancy_name") != t.tenancy_name:
        raise PreflightError(
            f"OCI profile {t.profile} tenancy {ident.get('tenancy_name')!r} != configured {t.tenancy_name!r}")
    if ident.get("compartment_name") != t.compartment_name:
        raise PreflightError(
            f"compartment {t.compartment_name!r} not found/matching under profile {t.profile}")


def sdk_identity(profile: str, compartment_name: str) -> dict:  # pragma: no cover - needs OCI
    import oci  # lazy

    cfg = oci.config.from_file(profile_name=profile)
    idc = oci.identity.IdentityClient(cfg)
    tenancy = idc.get_tenancy(cfg["tenancy"]).data
    # Paginate: list_compartments returns one page only; subtree listing needs access_level=ANY.
    comps = oci.pagination.list_call_get_all_results(
        idc.list_compartments, cfg["tenancy"], compartment_id_in_subtree=True,
        access_level="ANY", lifecycle_state="ACTIVE").data
    found = next((c.name for c in comps if c.name == compartment_name), None)
    return {"tenancy_name": tenancy.name, "compartment_name": found}
