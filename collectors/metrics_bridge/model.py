"""Data model shared by the metrics bridge modules."""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple


class DropReason(str, enum.Enum):
    stale = "stale"
    future = "future"
    cardinality = "cardinality"
    too_many_dims = "too_many_dims"
    invalid_namespace = "invalid_namespace"
    unsupported_type = "unsupported_type"


@dataclass(frozen=True)
class MetricPoint:
    namespace: str
    name: str
    dimensions: Tuple[Tuple[str, str], ...]
    timestamp: datetime
    value: float
    unit: Optional[str] = None
    resource_group: Optional[str] = None

    @property
    def stream_key(self) -> tuple:
        return (self.namespace, self.resource_group, self.name, self.dimensions)
