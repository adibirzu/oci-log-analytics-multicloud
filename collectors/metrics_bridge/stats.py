"""Self-metric counters of the bridge (no dimension values are ever stored)."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .model import DropReason


@dataclass
class BridgeStats:
    dropped_total: Counter = field(default_factory=Counter)  # DropReason -> n
    posted_total: int = 0   # metric streams accepted by OCI Monitoring
    failed_total: int = 0   # metric streams rejected / failed after retries
    batches_total: int = 0  # PostMetricData calls that completed (any outcome)

    def drop(self, reason: DropReason, n: int = 1) -> None:
        self.dropped_total[reason] += n

    def snapshot(self) -> dict:
        return {
            "dropped_total": {r.value: n for r, n in self.dropped_total.items()},
            "posted_total": self.posted_total,
            "failed_total": self.failed_total,
            "batches_total": self.batches_total,
        }
