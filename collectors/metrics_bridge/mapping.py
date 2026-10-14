"""Pure mapping rules: OTLP resource/point attributes -> OCI Monitoring shape."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Mapping, Optional, Tuple

from .model import DropReason

MAX_NAMESPACE_LEN = 255
MAX_METRIC_NAME_LEN = 255
MAX_DIMENSIONS = 20
MAX_DIM_VALUE_LEN = 256
MAX_DIM_KEY_LEN = 256
MAX_AGE = timedelta(hours=2)
MAX_FUTURE = timedelta(minutes=10)
DEFAULT_MAX_STREAMS_PER_NAMESPACE = 5000
RESERVED_NAMESPACE_PREFIXES = ("oci_", "oracle_")

NAMESPACE_RE = re.compile(r"^[a-z0-9_]+$")
METRIC_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_NS_BAD = re.compile(r"[^a-z0-9_]+")
_NAME_BAD = re.compile(r"[^A-Za-z0-9._-]+")
_DIM_KEY_BAD = re.compile(r"[^A-Za-z0-9._-]+")

# Resource attributes always kept as dimensions, in fixed priority order.
RESOURCE_DIM_KEYS = (
    "cloud.provider",
    "cloud.account.id",
    "cloud.region",
    "service.name",
    "cloud.resource_id",
)
HASHED_KEYS = frozenset({"cloud.resource_id"})

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sanitize_namespace(cloud: str, service: str) -> str:
    """`custom_<cloud>_<service>`; always safe, may still fail validate_namespace."""
    raw = f"custom_{cloud or ''}_{service or ''}".lower()
    ns = _NS_BAD.sub("_", raw)
    ns = re.sub(r"_+", "_", ns).strip("_")
    return ns[:MAX_NAMESPACE_LEN]


def validate_namespace(ns: str) -> bool:
    return (
        bool(ns)
        and len(ns) <= MAX_NAMESPACE_LEN
        and NAMESPACE_RE.match(ns) is not None
        and not ns.startswith(RESERVED_NAMESPACE_PREFIXES)
    )


def sanitize_metric_name(name: str) -> str:
    out = _NAME_BAD.sub("_", name or "")
    out = re.sub(r"_+", "_", out).strip("_")
    return out[:MAX_METRIC_NAME_LEN]


def validate_metric_name(name: str) -> bool:
    return bool(name) and len(name) <= MAX_METRIC_NAME_LEN and METRIC_NAME_RE.match(name) is not None


def _hash12(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _dim_value(key: str, value) -> str:
    s = value if isinstance(value, str) else str(value)
    if key in HASHED_KEYS:
        s = _hash12(s)
    return s[:MAX_DIM_VALUE_LEN]


def build_dimensions(
    resource_attrs: Mapping[str, object],
    point_attrs: Optional[Mapping[str, object]] = None,
) -> Tuple[Tuple[Tuple[str, str], ...], bool]:
    """Return (sorted dimension tuple, trimmed?).

    The five resource keys are kept first (fixed order); point attributes follow
    sorted by key and are the first to be dropped when over MAX_DIMENSIONS.
    At least one dimension is guaranteed (`cloud.provider`, default "unknown").
    """
    dims: Dict[str, str] = {}
    for key in RESOURCE_DIM_KEYS:
        val = resource_attrs.get(key)
        if val is not None and str(val) != "":
            dims[key] = _dim_value(key, val)
    extras = []
    for key in sorted((point_attrs or {}).keys()):
        val = point_attrs[key]
        clean = _DIM_KEY_BAD.sub("_", str(key))[:MAX_DIM_KEY_LEN]
        if not clean or val is None or str(val) == "" or clean in dims:
            continue
        extras.append((clean, _dim_value(clean, val)))
    room = MAX_DIMENSIONS - len(dims)
    trimmed = len(extras) > room
    for key, val in extras[: max(room, 0)]:
        dims[key] = val
    if not dims:
        dims["cloud.provider"] = "unknown"
    return tuple(sorted(dims.items())), trimmed


def check_timestamp(ts: datetime, now: datetime) -> Optional[DropReason]:
    if ts < now - MAX_AGE:
        return DropReason.stale
    if ts > now + MAX_FUTURE:
        return DropReason.future
    return None


def namespace_for_resource(resource_attrs: Mapping[str, object]) -> str:
    return sanitize_namespace(
        str(resource_attrs.get("cloud.provider") or "unknown"),
        str(resource_attrs.get("service.name") or "unknown"),
    )


class CardinalityLimiter:
    """Caps distinct streams per namespace within an interval; call reset() per interval."""

    def __init__(self, max_streams_per_namespace: int = DEFAULT_MAX_STREAMS_PER_NAMESPACE):
        self.max_streams = max_streams_per_namespace
        self._seen: Dict[str, set] = {}
        self.dropped: Dict[str, int] = {}

    def allow(self, namespace: str, name: str, dims: tuple) -> bool:
        seen = self._seen.setdefault(namespace, set())
        key = (name, dims)
        if key in seen:
            return True
        if len(seen) >= self.max_streams:
            self.dropped[namespace] = self.dropped.get(namespace, 0) + 1
            return False
        seen.add(key)
        return True

    def reset(self) -> None:
        self._seen.clear()
