from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from collectors.metrics_bridge.model import MetricPoint
from collectors.metrics_bridge.post import MonitoringPoster, TokenBucket
from collectors.metrics_bridge.stats import BridgeStats

TS = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
COMP = "<COMPARTMENT_OCID>"


class FakeErr(Exception):
    def __init__(self, status):
        self.status = status


class FakeClient:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = []

    def post_metric_data(self, details):
        self.calls.append(details)
        step = self.script.pop(0) if self.script else None
        if isinstance(step, Exception):
            raise step
        failed = step or 0
        return SimpleNamespace(data=SimpleNamespace(failed_metrics_count=failed, failed_metrics=[]))


def points(n, unit="ms"):
    return [MetricPoint("custom_aws_ec2", f"m{i}", (("cloud.provider", "aws"),), TS, float(i), unit)
            for i in range(n)]


def poster(client, **kw):
    sleeps = []
    stats = BridgeStats()
    bucket = TokenBucket(rate=1000, clock=lambda: 0.0, sleep=lambda s: None)
    p = MonitoringPoster(client, COMP, stats=stats, bucket=bucket, sleep=sleeps.append,
                         rng=lambda: 1.0, **kw)
    return p, stats, sleeps


def test_120_streams_three_calls():
    c = FakeClient()
    p, stats, _ = poster(c)
    p.post(points(120))
    assert [len(x.metric_data) for x in c.calls] == [50, 50, 20]
    md = c.calls[0].metric_data[0]
    assert md.compartment_id == COMP and md.metadata == {"unit": "ms"}
    assert md.dimensions == {"cloud.provider": "aws"} and md.datapoints[0].timestamp == TS
    assert (stats.batches_total, stats.posted_total, stats.failed_total) == (3, 120, 0)


def test_same_stream_points_merge():
    c = FakeClient()
    p, _, _ = poster(c)
    p.post(points(1) + points(1))
    assert len(c.calls[0].metric_data) == 1 and len(c.calls[0].metric_data[0].datapoints) == 2


def test_429_then_success_retried_with_backoff():
    c = FakeClient([FakeErr(429), FakeErr(503), 0])
    p, stats, sleeps = poster(c)
    p.post(points(1))
    assert len(c.calls) == 3 and sleeps == [0.5, 1.0]
    assert (stats.posted_total, stats.failed_total, stats.batches_total) == (1, 0, 1)


def test_400_not_retried_and_counted():
    c = FakeClient([FakeErr(400)])
    p, stats, sleeps = poster(c)
    p.post(points(3))
    assert len(c.calls) == 1 and sleeps == []
    assert (stats.failed_total, stats.posted_total) == (3, 0)


def test_retries_exhausted():
    c = FakeClient([FakeErr(500)] * 10)
    p, stats, _ = poster(c, max_retries=5)
    p.post(points(2))
    assert len(c.calls) == 6 and stats.failed_total == 2


def test_partial_failure_counted():
    c = FakeClient([2])
    p, stats, _ = poster(c)
    p.post(points(5))
    assert (stats.posted_total, stats.failed_total) == (3, 2)


def test_token_bucket_pacing_fake_clock():
    t = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s
    b = TokenBucket(rate=2, capacity=2, clock=lambda: t[0], sleep=sleep)
    for _ in range(4):
        b.acquire()
    assert slept == pytest.approx([0.5, 0.5])  # 2 burst, then 2 req/s
    t[0] += 10
    b.acquire()
    assert len(slept) == 2  # refilled, no wait
