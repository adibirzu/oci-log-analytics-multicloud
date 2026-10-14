"""AWS CloudTrail transport with injectable AWS and OCI clients."""

from .config import CollectorConfig
from .normalize import InvalidMessage, batch_ndjson, normalize_record
from .pipeline import Collector

__all__ = ["Collector", "CollectorConfig", "InvalidMessage", "batch_ndjson", "normalize_record"]
