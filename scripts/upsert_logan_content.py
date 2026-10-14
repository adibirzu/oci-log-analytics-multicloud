#!/usr/bin/env python3
"""Plan or apply a selected checked-in cloud Logan content bundle.

The module deliberately keeps the OCI client at the edge: tests can inject a
fake client and exercise the planner without credentials or network access.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
CONTENT = ROOT / "content" / "logan"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


BUNDLES = ("aws_cloudtrail_ocsf", "azure_activity_ocsf", "azure_entra_signin_ocsf", "azure_entra_audit_ocsf", "gcp_audit_ocsf", "gcp_vpc_flow_ocsf", "gcp_gke_audit_ocsf")


@dataclass(frozen=True)
class Resource:
    kind: str
    display_name: str
    name: str | None
    body: dict[str, Any]


def load_content(directory: Path = CONTENT, *, bundle: str = "aws_cloudtrail_ocsf") -> tuple[Resource, ...]:
    """Load and validate the three canonical content files."""
    if bundle not in BUNDLES:
        raise ValueError("unsupported Logan content bundle")
    fields = json.loads((directory / f"{bundle}.fields.json").read_text())
    parser = json.loads((directory / f"{bundle}.parser.json").read_text())
    source = json.loads((directory / f"{bundle}.source.json").read_text())
    if not isinstance(fields.get("fields"), list) or not isinstance(parser.get("parser"), dict):
        raise ValueError("invalid Logan content bundle")
    return tuple(
        [Resource("field", x["display_name"], x.get("name"), x) for x in fields["fields"]]
        + [Resource("parser", parser["parser"]["display_name"], parser["parser"].get("name"), parser["parser"])]
        + [Resource("source", source["source"]["display_name"], source["source"].get("name"), source["source"])]
    )


def _items(response: Any) -> list[Any]:
    data = getattr(response, "data", response)
    if isinstance(data, list):
        return data
    if isinstance(data, Mapping):
        return list(data.get("items", data.get("results", [])))
    return list(getattr(data, "items", []) or [])


def _next(response: Any) -> str | None:
    headers = getattr(response, "headers", {}) or {}
    return headers.get("opc-next-page")


def _list_all(method: Callable[..., Any], *args: Any, **kwargs: Any) -> list[Any]:
    result: list[Any] = []
    page = None
    seen = set()
    for _ in range(1000):
        call_kwargs = dict(kwargs)
        if page:
            call_kwargs["page"] = page
        response = method(*args, **call_kwargs)
        result.extend(_items(response))
        page = _next(response)
        if not page:
            return result
        if page in seen:
            raise RuntimeError("content discovery repeated a page")
        seen.add(page)
    raise RuntimeError("content discovery exceeded page bound")


def _attr(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _plain(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return _plain(value.to_dict())
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    types = getattr(value, "swagger_types", None)
    if isinstance(types, Mapping):
        return {key: _plain(getattr(value, key, None)) for key in types if getattr(value, key, None) is not None}
    return value


def _model(module: Any, class_name: str, payload: Any) -> Any:
    if isinstance(payload, list):
        return [_model(module, class_name, x) for x in payload]
    if not isinstance(payload, Mapping):
        return payload
    cls = getattr(module, class_name)
    types = cls().__dict__.get("swagger_types", {})
    converted = {}
    for key, value in payload.items():
        nested = types.get(key, "")
        if value is None:
            converted[key] = None
        elif nested.startswith("list["):
            converted[key] = [_model(module, nested[5:-1], x) for x in value]
        elif nested and not isinstance(value, (str, int, float, bool, type(None))):
            converted[key] = _model(module, nested, value)
        else:
            converted[key] = value
    return cls(**converted)


def _is_404(exc: Exception) -> bool:
    return getattr(exc, "status", None) == 404


def _etag(response: Any, resource: Any) -> str | None:
    headers = getattr(response, "headers", {}) or {}
    return headers.get("etag") or _attr(resource, "etag")


def _get_current(client: Any, kind: str, namespace: str, compartment: str, listed: Any) -> tuple[Any, str | None]:
    """Fetch the canonical resource so its server etag is available."""
    name = _attr(listed, "name")
    try:
        if kind == "field":
            response = client.get_field(namespace, name)
        elif kind == "parser":
            response = client.get_parser(namespace, name)
        else:
            response = client.get_source(namespace, name, compartment)
    except Exception as exc:
        if _is_404(exc):
            return None, None
        raise
    return getattr(response, "data", response), _etag(response, listed)


def _find_unique(items: list[Any], display_name: str, kind: str) -> Any | None:
    matches = [x for x in items if _attr(x, "display_name") == display_name]
    if len(matches) > 1:
        raise ValueError(f"duplicate {kind} display name: {display_name}")
    return matches[0] if matches else None


def _discover(client: Any, namespace: str, compartment: str) -> dict[str, list[Any]]:
    return {
        "field": _list_all(client.list_fields, namespace),
        "parser": _list_all(client.list_parsers, namespace),
        "source": _list_all(client.list_sources, namespace, compartment),
    }


def _current_by_display(client: Any, kind: str, namespace: str, compartment: str, items: list[Any], display_name: str) -> tuple[Any | None, str | None]:
    listed = _find_unique(items, display_name, kind)
    if listed is None:
        return None, None
    return _get_current(client, kind, namespace, compartment, listed)


def _matches(existing: Any, desired: Any) -> bool:
    """Compare owned properties, ignoring provider-generated nested metadata."""
    if isinstance(desired, Mapping):
        return isinstance(existing, Mapping) and all(_matches(existing.get(key), value) for key, value in desired.items())
    if isinstance(desired, list):
        return isinstance(existing, list) and len(existing) == len(desired) and all(
            _matches(actual, expected) for actual, expected in zip(existing, desired)
        )
    return existing == desired


def _desired_body(item: Resource, names: Mapping[str, str]) -> dict[str, Any]:
    body = copy.deepcopy(item.body)
    if item.kind == "parser":
        for mapping in body["field_maps"]:
            for key in ("parser_field_name", "storage_field_name", "parser_field_integrator_name"):
                if key in mapping:
                    mapping[key] = names.get(mapping[key], mapping[key])
    elif item.kind == "source":
        body["parsers"] = [{"name": names.get(name, name)} for name in body["parsers"]]
    return body


def _time_name(client: Any, namespace: str, compartment: str, found: Mapping[str, list[Any]]) -> str:
    resource, _ = _current_by_display(client, "field", namespace, compartment, found["field"], "Time")
    if resource is None or not _attr(resource, "name") or str(_attr(resource, "data_type", "")).upper() != "TIMESTAMP":
        raise RuntimeError("Log Analytics built-in Time field is unavailable")
    return _attr(resource, "name")


def _builtin_names(client: Any, namespace: str, compartment: str, found: Mapping[str, list[Any]], resources: tuple[Resource, ...]) -> dict[str, str]:
    """Resolve parser storage fields the bundle does not define to built-in fields.

    Built-in fields consume no custom-field quota and share names with
    the OCI sources, so cross-cloud queries use one vocabulary. A reference that
    is not a system field fails closed instead of minting a custom field.
    """
    names = {"Time": _time_name(client, namespace, compartment, found)}
    bundle = {x.display_name for x in resources if x.kind == "field"} | {x.name for x in resources if x.kind == "field" and x.name}
    for item in resources:
        if item.kind != "parser":
            continue
        for mapping in item.body.get("field_maps", []):
            ref = mapping.get("storage_field_name")
            if not ref or ref in bundle or ref in names:
                continue
            resource = _find_unique(found["field"], ref, "field")
            if resource is None or not _attr(resource, "is_system") or not _attr(resource, "name"):
                raise RuntimeError(f"built-in Log Analytics field is unavailable: {ref}")
            names[ref] = _attr(resource, "name")
    return names

_WRITE_METHOD = {"field": "upsert_field", "parser": "upsert_parser", "source": "upsert_source"}
_MODEL_NAME = {"field": "UpsertLogAnalyticsFieldDetails", "parser": "UpsertLogAnalyticsParserDetails", "source": "UpsertLogAnalyticsSourceDetails"}


@dataclass(frozen=True)
class PlannedItem:
    resource: Resource
    action: str  # "create" | "update" | "unchanged"
    current: Any
    etag: str | None


def _fetch_checked(client: Any, kind: str, namespace: str, compartment: str, found: Mapping[str, list[Any]], display_name: str) -> tuple[Any | None, str | None]:
    """Read one content resource and refuse anything not safe to write."""
    current, etag = _current_by_display(client, kind, namespace, compartment, found[kind], display_name)
    if current and (not etag or not _attr(current, "name")):
        raise RuntimeError("content resource lacks its name or concurrency token")
    if current and _attr(current, "is_system"):
        raise RuntimeError("cannot modify a built-in resource")
    return current, etag


def _is_unchanged(current: Any, desired: Mapping[str, Any]) -> bool:
    return _matches(_plain(current), desired)


def _resolve_plan(client: Any, namespace: str, compartment: str, resources: tuple[Resource, ...]) -> tuple[dict[str, str], list[PlannedItem]]:
    """Read-only: build the name map and a per-item create/update/unchanged decision."""
    found = _discover(client, namespace, compartment)
    names = _builtin_names(client, namespace, compartment, found, resources)
    currents = {}
    for item in resources:
        current, etag = _fetch_checked(client, item.kind, namespace, compartment, found, item.display_name)
        currents[(item.kind, item.display_name)] = (current, etag)
        if current:
            names[item.name or item.display_name] = _attr(current, "name")
            names[item.display_name] = _attr(current, "name")
    planned = []
    for item in resources:
        current, etag = currents[(item.kind, item.display_name)]
        body = _desired_body(item, names)
        body.pop("name", None)
        if current is None:
            action = "create"
        else:
            action = "unchanged" if _is_unchanged(current, body) else "update"
        planned.append(PlannedItem(item, action, current, etag))
    return names, planned


def _summary(planned: list[PlannedItem]) -> dict[str, Any]:
    actions = [
        {"kind": x.resource.kind, "display_name": x.resource.display_name, "action": x.action,
         "has_concurrency_token": bool(x.etag)}
        for x in planned
    ]
    return {"actions": actions, "counts": {a: sum(x["action"] == a for x in actions) for a in ("create", "update", "unchanged")}}


def plan_content(client: Any, namespace: str, compartment: str, resources: tuple[Resource, ...] | None = None) -> dict[str, Any]:
    """Dry-run: compute the per-item plan. Issues list/get calls only."""
    resources = resources or load_content()
    _, planned = _resolve_plan(client, namespace, compartment, resources)
    return _summary(planned)


def apply_content(client: Any, namespace: str, compartment: str, resources: tuple[Resource, ...] | None = None) -> dict[str, Any]:
    """Apply fields, then parser, then source. Each item uses the planner's decision.

    Items are read and written in content order, so parser and source bodies
    resolve against names generated by earlier writes in this run. Unchanged
    items are never written. No retries after conflicts.
    """
    resources = resources or load_content()
    import oci.log_analytics.models as models

    found = _discover(client, namespace, compartment)
    names: dict[str, str] = _builtin_names(client, namespace, compartment, found, resources)
    counts = {"created": 0, "updated": 0, "unchanged": 0}
    for item in resources:
        kind = item.kind
        current, current_etag = _fetch_checked(client, kind, namespace, compartment, found, item.display_name)
        if current is None:
            # Fail closed if the identity appeared since discovery.
            fresh = _discover(client, namespace, compartment)[kind]
            if _find_unique(fresh, item.display_name, kind) is not None:
                raise RuntimeError(f"{kind} {item.display_name} appeared after planning; re-run dry-run")
        body = _desired_body(item, names)
        if current:
            existing_name = _attr(current, "name")
            names[item.display_name] = existing_name
            if item.name:
                names[item.name] = existing_name
            body["name"] = existing_name
            if kind in {"parser", "source"}:
                body["edit_version"] = _attr(current, "edit_version")
            desired = {key: value for key, value in body.items() if key not in {"edit_version", "name"}}
            if _is_unchanged(current, desired):
                counts["unchanged"] += 1
                continue
            if not current_etag:
                raise RuntimeError(f"missing etag for {kind} {item.display_name}")
        elif kind == "field":
            # The service generates custom field names; parsers and sources
            # must be created with the bundle's stable name.
            body.pop("name", None)
            body.pop("edit_version", None)
        elif not body.get("name"):
            raise RuntimeError(f"{kind} {item.display_name} needs a stable name to be created")
        identity = json.dumps([namespace, compartment, kind, item.display_name, body], sort_keys=True, separators=(",", ":"))
        kwargs: dict[str, Any] = {"opc_retry_token": hashlib.sha256(identity.encode()).hexdigest()}
        if current:
            kwargs["if_match"] = current_etag
        response = getattr(client, _WRITE_METHOD[kind])(namespace, _model(models, _MODEL_NAME[kind], body), **kwargs)
        result = getattr(response, "data", response)
        result_name = _attr(result, "name") or body.get("name")
        if not result_name:
            raise RuntimeError(f"upsert returned no generated name for {kind}")
        names[item.display_name] = result_name
        if item.name:
            names[item.name] = result_name
        counts["updated" if current else "created"] += 1
    return {"counts": counts}


def build_client() -> Any:
    import oci
    from collectors.logs.runtime import suppress_sdk_payload_logging
    from collectors.common.oci_auth import build_oci_client
    suppress_sdk_payload_logging()
    auth_mode = os.getenv("OCI_AUTH_MODE", "resource_principal").strip().lower()
    from collectors.logs.config import allowed_oci_auth_modes, operator_auth_enabled
    if auth_mode not in allowed_oci_auth_modes(operator_auth_enabled(os.environ)):
        raise ValueError("OCI_AUTH_MODE must be instance_principal or resource_principal")
    return build_oci_client(oci.log_analytics.LogAnalyticsClient, auth_mode=auth_mode, region=os.getenv("OCI_REGION") or None, timeout=(10, 30))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", "--plan", dest="dry_run", action="store_true", help="print the create/update/unchanged plan without writes (default)")
    mode.add_argument("--apply", action="store_true", help="perform the planned creates and updates")
    parser.add_argument("--namespace", default=os.getenv("LOGAN_NAMESPACE", os.getenv("OCI_LA_NAMESPACE")))
    parser.add_argument("--compartment-id", default=os.getenv("LOGAN_COMPARTMENT_OCID", os.getenv("OCI_LA_COMPARTMENT")))
    parser.add_argument("--bundle", default="aws_cloudtrail_ocsf", choices=BUNDLES, help="content bundle to plan or apply")
    args = parser.parse_args(argv)
    if not args.namespace or not args.compartment_id:
        parser.error("--namespace/LOGAN_NAMESPACE and --compartment-id/LOGAN_COMPARTMENT_OCID are required")
    try:
        resources = load_content(bundle=args.bundle)
        run = apply_content if args.apply else plan_content
        result = run(build_client(), args.namespace, args.compartment_id, resources)
    except Exception as exc:
        # Class name only: messages may echo tenancy or namespace values.
        print(f"logan content operation failed: {exc.__class__.__name__}", file=sys.stderr)
        return 1
    result = {"mode": "apply" if args.apply else "dry-run", "bundle": args.bundle, **result}
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
