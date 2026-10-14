"""Logical Log Analytics source names and normalized field mappings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

SOURCE_CATALOG_PATH_ENV = "MC_LOGAN_SOURCES_PATH"
TENANT_ID_ENV = "MC_TENANT_ID"
DEFAULT_SOURCE_CATALOG_PATH = Path(__file__).resolve().parent / "logan_sources.yaml"
CATALOG_SCHEMA_VERSION = "mc.logan-sources.v1"
LOG_SOURCE_FIELD = "Log Source"


def source_field(*, quote: str = "'") -> str:
    """Return the quoted OCL field shared by predicates and aggregations."""
    if quote not in ("'", '"'):
        raise ValueError("quote must be a single or double quote")
    return f"{quote}{LOG_SOURCE_FIELD}{quote}"


@dataclass(frozen=True)
class LoganSourceCatalog:
    """Validated source catalog with optional tenant-specific overlays."""

    sources: Mapping[str, Mapping[str, Any]]
    tenants: Mapping[str, Mapping[str, Any]]

    def _entry(self, key: str, tenant_id: str | None = None) -> Mapping[str, Any]:
        if key not in self.sources:
            raise KeyError(f"unknown Log Analytics source key: {key}")
        entry = dict(self.sources[key])
        tenant_sources = (self.tenants.get(tenant_id or "", {}) or {}).get("sources", {}) or {}
        override = tenant_sources.get(key, {}) or {}
        if not isinstance(override, Mapping):
            raise ValueError(f"tenant source override for {key} must be a mapping")
        entry.update(override)
        if "fields" in override:
            entry["fields"] = {**self.sources[key].get("fields", {}), **override["fields"]}
        return entry

    def source_name(self, key: str, *, tenant_id: str | None = None) -> str:
        entry = self._entry(key, tenant_id)
        name = entry.get("display_name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Log Analytics source {key} has no display_name")
        return name.strip()

    def field_name(
        self, key: str, normalized_field: str, *, tenant_id: str | None = None
    ) -> str | None:
        fields = self._entry(key, tenant_id).get("fields", {}) or {}
        return fields.get(normalized_field)

    def status(self, key: str, *, tenant_id: str | None = None) -> str:
        return str(self._entry(key, tenant_id).get("status", "active"))


def _validate_document(document: Any) -> LoganSourceCatalog:
    if not isinstance(document, Mapping):
        raise ValueError("Log Analytics source catalog must be a mapping")
    if document.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise ValueError("unsupported Log Analytics source catalog schema_version")
    sources = document.get("sources") or {}
    tenants = document.get("tenants") or {}
    if not isinstance(sources, Mapping) or not isinstance(tenants, Mapping):
        raise ValueError("sources and tenants must be mappings")
    for key, entry in sources.items():
        if not isinstance(key, str) or not isinstance(entry, Mapping):
            raise ValueError("each Log Analytics source must be a mapping")
        if not isinstance(entry.get("display_name"), str) or not entry["display_name"].strip():
            raise ValueError(f"Log Analytics source {key} must define display_name")
        fields = entry.get("fields", {}) or {}
        if not isinstance(fields, Mapping) or any(
            not isinstance(field, str) or not isinstance(name, str)
            for field, name in fields.items()
        ):
            raise ValueError(f"Log Analytics source {key} fields must map strings to strings")
    return LoganSourceCatalog(dict(sources), dict(tenants))


@lru_cache(maxsize=8)
def _load_catalog_cached(path: str, modified_ns: int) -> LoganSourceCatalog:
    del modified_ns  # Included in the key so edits to mounted config take effect.
    import yaml

    document = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return _validate_document(document)


def load_catalog(path: str | Path | None = None) -> LoganSourceCatalog:
    """Load the default catalog or a tenant-overridable YAML file."""
    configured_path = path or os.getenv(SOURCE_CATALOG_PATH_ENV, "").strip()
    catalog_path = Path(configured_path) if configured_path else DEFAULT_SOURCE_CATALOG_PATH
    stat = catalog_path.stat()
    return _load_catalog_cached(str(catalog_path.resolve()), stat.st_mtime_ns)


def source_name(key: str, *, tenant_id: str | None = None) -> str:
    """Resolve a logical source key using an explicit or environment tenant."""
    return load_catalog().source_name(key, tenant_id=tenant_id or os.getenv(TENANT_ID_ENV))


def source_clause(
    *keys: str,
    tenant_id: str | None = None,
    quote: str = "'",
    in_operator: str = "in",
    separator: str = ", ",
) -> str:
    """Return an OCL source predicate for one or more logical source keys."""
    if not keys:
        raise ValueError("source_clause requires at least one logical source key")
    if quote not in ("'", '"'):
        raise ValueError("quote must be a single or double quote")
    if in_operator not in ("in", "IN") or separator not in (",", ", "):
        raise ValueError("unsupported source predicate formatting")
    names = [source_name(key, tenant_id=tenant_id) for key in keys]
    quoted = [quote + name.replace(quote, quote + quote) + quote for name in names]
    if len(quoted) == 1:
        return f"{source_field(quote=quote)} = {quoted[0]}"
    return f"{source_field(quote=quote)} {in_operator} (" + separator.join(quoted) + ")"
