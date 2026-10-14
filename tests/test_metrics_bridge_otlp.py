from datetime import datetime, timezone

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.metrics.v1 import metrics_pb2 as pb

from collectors.metrics_bridge import mapping
from collectors.metrics_bridge.model import DropReason
from collectors.metrics_bridge.otlp_decode import OtlpDecoder
from collectors.metrics_bridge.stats import BridgeStats

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
NS_NOW = int(NOW.timestamp() * 1e9)


def kv(k, v):
    return KeyValue(key=k, value=AnyValue(string_value=v))


def request(metrics, service="ec2"):
    req = ExportMetricsServiceRequest()
    rm = req.resource_metrics.add()
    rm.resource.attributes.extend([kv("cloud.provider", "aws"), kv("service.name", service),
                                   kv("cloud.region", "r1")])
    rm.scope_metrics.add().metrics.extend(metrics)
    return req.SerializeToString()


def decoder():
    stats = BridgeStats()
    return OtlpDecoder(stats=stats, clock=lambda: NOW), stats


def num(value, ts=NS_NOW, **attrs):
    dp = pb.NumberDataPoint(time_unix_nano=ts, as_double=value)
    dp.attributes.extend(kv(k, v) for k, v in attrs.items())
    return dp


def test_gauge_as_is():
    m = pb.Metric(name="cpu", unit="%", gauge=pb.Gauge(data_points=[num(3.5, host="h1")]))
    d, _ = decoder()
    (p,) = d.decode(request([m]))
    assert (p.namespace, p.name, p.value, p.unit) == ("custom_aws_ec2", "cpu", 3.5, "%")
    assert dict(p.dimensions)["host"] == "h1" and p.timestamp == NOW


def test_sum_cumulative_to_delta_with_reset():
    def req(v):
        s = pb.Sum(data_points=[num(v)], is_monotonic=True,
                   aggregation_temporality=pb.AGGREGATION_TEMPORALITY_CUMULATIVE)
        return request([pb.Metric(name="reqs", sum=s)])
    d, _ = decoder()
    assert d.decode(req(10)) == []                      # first sight: baseline only
    assert [p.value for p in d.decode(req(25))] == [15]
    assert [p.value for p in d.decode(req(4))] == [4]   # reset: value since restart
    assert [p.value for p in d.decode(req(9))] == [5]


def test_sum_delta_as_is_and_nonmonotonic_as_gauge():
    s1 = pb.Sum(data_points=[num(7)], is_monotonic=True,
                aggregation_temporality=pb.AGGREGATION_TEMPORALITY_DELTA)
    s2 = pb.Sum(data_points=[num(-2)], is_monotonic=False,
                aggregation_temporality=pb.AGGREGATION_TEMPORALITY_CUMULATIVE)
    d, _ = decoder()
    pts = d.decode(request([pb.Metric(name="a", sum=s1), pb.Metric(name="b", sum=s2)]))
    assert [(p.name, p.value) for p in pts] == [("a", 7), ("b", -2)]


def test_histogram_expands():
    h = pb.HistogramDataPoint(time_unix_nano=NS_NOW, count=4, sum=10.0, min=1.0, max=5.0)
    h2 = pb.HistogramDataPoint(time_unix_nano=NS_NOW, count=2, sum=3.0)  # no min/max
    d, _ = decoder()
    pts = d.decode(request([pb.Metric(name="lat", histogram=pb.Histogram(data_points=[h, h2]))]))
    assert [(p.name, p.value) for p in pts] == [
        ("lat_count", 4), ("lat_sum", 10), ("lat_min", 1), ("lat_max", 5),
        ("lat_count", 2), ("lat_sum", 3)]


def test_summary_quantiles():
    s = pb.SummaryDataPoint(time_unix_nano=NS_NOW, count=3, sum=9.0)
    s.quantile_values.add(quantile=0.5, value=2.0)
    s.quantile_values.add(quantile=0.99, value=8.0)
    d, _ = decoder()
    pts = d.decode(request([pb.Metric(name="rt", summary=pb.Summary(data_points=[s]))]))
    assert [p.name for p in pts] == ["rt", "rt"]  # first sight: totals are baseline only
    assert [dict(p.dimensions)["quantile"] for p in pts] == ["0.5", "0.99"]


def test_exponential_histogram_count_sum_only():
    e = pb.ExponentialHistogramDataPoint(time_unix_nano=NS_NOW, count=5, sum=2.0, min=1.0)
    d, _ = decoder()
    pts = d.decode(request([pb.Metric(name="x", exponential_histogram=pb.ExponentialHistogram(data_points=[e]))]))
    assert [p.name for p in pts] == ["x_count", "x_sum"]


def test_unsupported_and_drops_counted():
    d, stats = decoder()
    stale = num(1, ts=NS_NOW - int(3 * 3600 * 1e9))
    future = num(1, ts=NS_NOW + int(3600 * 1e9))
    ok = num(1)
    pts = d.decode(request([pb.Metric(name="empty"),
                            pb.Metric(name="g", gauge=pb.Gauge(data_points=[stale, future, ok]))]))
    assert len(pts) == 1
    c = stats.dropped_total
    assert c[DropReason.unsupported_type] == 1 and c[DropReason.stale] == 1 and c[DropReason.future] == 1


def test_oci_prefixed_service_still_custom_namespace():
    d, _ = decoder()
    (p,) = d.decode(request([pb.Metric(name="g", gauge=pb.Gauge(data_points=[num(1)]))], service="oci_x"))
    assert p.namespace == "custom_aws_oci_x"


def test_cardinality_cap_and_dim_trim_counted():
    stats = BridgeStats()
    d = OtlpDecoder(stats=stats, clock=lambda: NOW, limiter=mapping.CardinalityLimiter(2))
    dps = [num(1, i=str(i)) for i in range(4)]
    d.decode(request([pb.Metric(name="g", gauge=pb.Gauge(data_points=dps))]))
    assert stats.dropped_total[DropReason.cardinality] == 2
    many = num(1, **{f"a{i:02d}": "x" for i in range(30)})
    d.limiter.reset()
    d.decode(request([pb.Metric(name="t", gauge=pb.Gauge(data_points=[many]))]))
    assert stats.dropped_total[DropReason.too_many_dims] == 1


def hist_request(*dps, temporality=pb.AGGREGATION_TEMPORALITY_CUMULATIVE):
    h = pb.Histogram(data_points=list(dps), aggregation_temporality=temporality)
    return request([pb.Metric(name="lat", unit="ms", histogram=h)])


def test_cumulative_histogram_count_and_sum_become_deltas():
    d, _ = decoder()
    first = pb.HistogramDataPoint(time_unix_nano=NS_NOW, count=4, sum=10.0, min=1.0, max=5.0)
    baseline = d.decode(hist_request(first))  # count/sum: baseline only; min/max as-is
    assert {p.name for p in baseline} == {"lat_min", "lat_max"}
    later = pb.HistogramDataPoint(time_unix_nano=NS_NOW, count=10, sum=31.0, min=0.5, max=9.0)
    pts = d.decode(hist_request(later))
    got = {p.name: p.value for p in pts}
    assert got == {"lat_count": 6, "lat_sum": 21.0, "lat_min": 0.5, "lat_max": 9.0}
    assert all(p.unit == "ms" for p in pts)


def test_cumulative_histogram_reset_emits_new_total():
    d, _ = decoder()
    d.decode(hist_request(pb.HistogramDataPoint(time_unix_nano=NS_NOW, count=10, sum=30.0)))
    pts = d.decode(hist_request(pb.HistogramDataPoint(time_unix_nano=NS_NOW, count=3, sum=7.0)))
    assert {p.name: p.value for p in pts} == {"lat_count": 3, "lat_sum": 7.0}


def hist_dp(count, **attrs):
    dp = pb.HistogramDataPoint(time_unix_nano=NS_NOW, count=count, sum=float(count))
    dp.attributes.extend(kv(k, v) for k, v in attrs.items())
    return dp


def test_cumulative_histogram_series_tracked_per_attributes():
    d, _ = decoder()
    d.decode(hist_request(hist_dp(2, host="a"), hist_dp(5, host="b")))
    pts = d.decode(hist_request(hist_dp(3, host="a"), hist_dp(5, host="b")))
    counts = [(dict(p.dimensions)["host"], p.value) for p in pts if p.name == "lat_count"]
    assert counts == [("a", 1), ("b", 0)]  # unchanged series still reports a zero delta


def test_delta_histogram_unchanged():
    d, _ = decoder()
    pts = d.decode(hist_request(pb.HistogramDataPoint(time_unix_nano=NS_NOW, count=4, sum=8.0),
                                temporality=pb.AGGREGATION_TEMPORALITY_DELTA))
    assert [(p.name, p.value) for p in pts] == [("lat_count", 4), ("lat_sum", 8.0)]
    again = d.decode(hist_request(pb.HistogramDataPoint(time_unix_nano=NS_NOW, count=4, sum=8.0),
                                  temporality=pb.AGGREGATION_TEMPORALITY_DELTA))
    assert [p.value for p in again] == [4, 8.0]


def test_summary_count_and_sum_are_deltas():
    def req(count, total):
        s = pb.SummaryDataPoint(time_unix_nano=NS_NOW, count=count, sum=total)
        s.quantile_values.add(quantile=0.5, value=2.0)
        return request([pb.Metric(name="rt", summary=pb.Summary(data_points=[s]))])
    d, _ = decoder()
    first = d.decode(req(3, 9.0))
    assert [p.name for p in first] == ["rt"]  # quantiles pass through; totals only baseline
    pts = d.decode(req(5, 15.0))
    assert [(p.name, p.value) for p in pts] == [("rt_count", 2), ("rt_sum", 6.0), ("rt", 2.0)]


def test_defaults_fill_missing_resource_attributes():
    req = ExportMetricsServiceRequest()
    rm = req.resource_metrics.add()
    rm.scope_metrics.add().metrics.extend(
        [pb.Metric(name="g", gauge=pb.Gauge(data_points=[num(1)]))])
    d = OtlpDecoder(stats=BridgeStats(), clock=lambda: NOW,
                    defaults={"cloud.provider": "gcp", "service.name": "gke"})
    (p,) = d.decode(req.SerializeToString())
    assert p.namespace == "custom_gcp_gke"
