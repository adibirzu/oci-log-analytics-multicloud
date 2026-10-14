"""CloudTrail to OCSF-aligned fields; raw provider payloads never pass through."""

from __future__ import annotations

import ipaddress
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Mapping


class InvalidMessage(ValueError):
    """A deterministic malformed input; errors deliberately contain no payload."""


def _string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 16384:
        raise InvalidMessage("invalid string field")
    return value or None


def normalize_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Map the catalog's normalized keys into nested JSON objects.

    This is OCSF-aligned naming, not a complete OCSF event-class implementation.
    AWS hostnames in sourceIPAddress remain in source_address, never in an IP field.
    """
    if not isinstance(record, Mapping):
        raise InvalidMessage("record is not an object")
    required = {key: _string(record.get(key)) for key in ("eventTime", "eventName", "eventID", "recipientAccountId", "awsRegion")}
    if any(value is None for value in required.values()):
        raise InvalidMessage("required event field missing")
    if not re.fullmatch(r"\d{12}", required["recipientAccountId"]):
        raise InvalidMessage("invalid record account")
    try:
        timestamp = datetime.fromisoformat(required["eventTime"].replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError
        timestamp = timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        raise InvalidMessage("invalid event timestamp") from None
    identity = record.get("userIdentity") or {}
    if not isinstance(identity, Mapping):
        raise InvalidMessage("invalid identity")
    context = identity.get("sessionContext") or {}
    if not isinstance(context, Mapping) or not isinstance(context.get("sessionIssuer", {}), Mapping):
        raise InvalidMessage("invalid identity context")
    issuer = context.get("sessionIssuer") or {}
    user = (_string(identity.get("userName")) or _string(issuer.get("userName"))
            or _string(identity.get("invokedBy")) or _string(identity.get("arn")) or _string(identity.get("principalId")))
    address = _string(record.get("sourceIPAddress"))
    try:
        ip = str(ipaddress.ip_address(address)) if address else None
    except ValueError:
        ip = None
    resources = record.get("resources") or []
    if not isinstance(resources, list) or any(not isinstance(item, Mapping) for item in resources):
        raise InvalidMessage("invalid resources")
    arns = [_string(item.get("ARN") or item.get("arn")) for item in resources]
    error = _string(record.get("errorCode"))
    return {
        "timestamp": timestamp,
        "cloud": {"provider": "aws", "account": {"uid": required["recipientAccountId"]}, "region": required["awsRegion"]},
        "actor": {"user": {"name": user}}, "src_endpoint": {"ip": ip},
        "activity_name": required["eventName"], "status": "Failure" if error or record.get("errorMessage") else "Success",
        "resource": ", ".join(arn for arn in arns if arn) or None,
        "trace_id": _string(record.get("requestID")), "event_uid": required["eventID"],
        "service_name": _string(record.get("eventSource")), "source_address": address, "error_code": error,
    }


def batch_ndjson(records: Iterable[Mapping[str, Any]], *, max_records: int, max_bytes: int) -> Iterator[bytes]:
    """Bound both UTF-8 upload size and record count, including newlines."""
    if max_records < 1 or max_bytes < 1:
        raise ValueError("batch limits must be positive")
    lines: list[bytes] = []
    size = 0
    for record in records:
        line = (json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        if len(line) > max_bytes:
            raise InvalidMessage("record exceeds batch size")
        if lines and (len(lines) >= max_records or size + len(line) > max_bytes):
            yield b"".join(lines)
            lines, size = [], 0
        lines.append(line)
        size += len(line)
    if lines:
        yield b"".join(lines)
