"""Batch MetricPoints into OCI Monitoring PostMetricData calls."""
from __future__ import annotations

import logging
import random
import time
from typing import Callable, Dict, List, Optional, Sequence

from .model import MetricPoint
from .stats import BridgeStats

log = logging.getLogger(__name__)

MAX_STREAMS_PER_CALL = 50
DEFAULT_RATE_PER_SEC = 20.0  # tenancy limit is 50 TPS; leave headroom for other writers
MAX_RETRIES = 5
BACKOFF_BASE_SEC = 0.5
BACKOFF_CAP_SEC = 30.0


class TokenBucket:
    def __init__(self, rate: float = DEFAULT_RATE_PER_SEC, capacity: Optional[float] = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.rate = rate
        self.capacity = capacity if capacity is not None else rate
        self.clock, self.sleep = clock, sleep
        self._tokens = self.capacity
        self._last = clock()

    def acquire(self) -> None:
        now = self.clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now
        if self._tokens < 1:
            self.sleep((1 - self._tokens) / self.rate)
            now = self.clock()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now
        self._tokens -= 1


def build_monitoring_client(region: str, auth: str = "resource_principal",
                            config_file: Optional[str] = None, profile: str = "DEFAULT"):
    """Lazy real client. auth: resource_principal | instance_principal | config (operator)."""
    import oci  # imported lazily so tests and decoding need no SDK setup

    endpoint = f"https://telemetry-ingestion.{region}.oraclecloud.com"
    if auth == "resource_principal":
        signer = oci.auth.signers.get_resource_principals_signer()
        return oci.monitoring.MonitoringClient({}, signer=signer, service_endpoint=endpoint)
    if auth == "instance_principal":
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        return oci.monitoring.MonitoringClient({}, signer=signer, service_endpoint=endpoint)
    if auth == "config":
        cfg = oci.config.from_file(config_file or oci.config.DEFAULT_LOCATION, profile)
        return oci.monitoring.MonitoringClient(cfg, service_endpoint=endpoint)
    raise ValueError(f"unknown auth mode: {auth}")


def _group_streams(points: Sequence[MetricPoint]) -> List[List[MetricPoint]]:
    streams: Dict[tuple, List[MetricPoint]] = {}
    for p in points:
        streams.setdefault(p.stream_key, []).append(p)
    return list(streams.values())


class MonitoringPoster:
    def __init__(self, client, compartment_id: str, stats: Optional[BridgeStats] = None,
                 bucket: Optional[TokenBucket] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 rng: Callable[[], float] = random.random,
                 max_retries: int = MAX_RETRIES,
                 batch_size: int = MAX_STREAMS_PER_CALL) -> None:
        self.client, self.compartment_id = client, compartment_id
        self.stats = stats or BridgeStats()
        self.bucket = bucket or TokenBucket()
        self.sleep, self.rng = sleep, rng
        self.max_retries, self.batch_size = max_retries, min(batch_size, MAX_STREAMS_PER_CALL)

    def _details(self, streams: List[List[MetricPoint]]):
        from oci.monitoring.models import Datapoint, MetricDataDetails, PostMetricDataDetails

        data = []
        for pts in streams:
            first = pts[0]
            data.append(MetricDataDetails(
                namespace=first.namespace,
                resource_group=first.resource_group,
                compartment_id=self.compartment_id,
                name=first.name,
                dimensions=dict(first.dimensions),
                metadata={"unit": first.unit} if first.unit else None,
                datapoints=[Datapoint(timestamp=p.timestamp, value=p.value) for p in pts],
            ))
        return PostMetricDataDetails(metric_data=data)

    def post(self, points: Sequence[MetricPoint]) -> None:
        streams = _group_streams(points)
        for i in range(0, len(streams), self.batch_size):
            self._post_batch(streams[i:i + self.batch_size])

    def _post_batch(self, streams: List[List[MetricPoint]]) -> None:
        details = self._details(streams)
        attempt = 0
        while True:
            self.bucket.acquire()
            try:
                resp = self.client.post_metric_data(details)
            except Exception as exc:  # noqa: BLE001 - classify below
                status = getattr(exc, "status", None)
                retryable = status is None or status == 429 or status >= 500
                if retryable and attempt < self.max_retries:
                    delay = min(BACKOFF_CAP_SEC, BACKOFF_BASE_SEC * (2 ** attempt))
                    self.sleep(delay * (0.5 + 0.5 * self.rng()))
                    attempt += 1
                    continue
                self.stats.batches_total += 1
                self.stats.failed_total += len(streams)
                log.warning("PostMetricData failed status=%s streams=%d", status, len(streams))
                return
            self.stats.batches_total += 1
            data = getattr(resp, "data", None)
            failed = int(getattr(data, "failed_metrics_count", 0) or 0)
            failed = min(failed, len(streams))
            self.stats.failed_total += failed
            self.stats.posted_total += len(streams) - failed
            if failed:
                log.warning("PostMetricData partial failure failed=%d of %d", failed, len(streams))
            return
