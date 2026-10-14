"""Decode OTLP ExportMetricsServiceRequest bytes into MetricPoints."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.metrics.v1 import metrics_pb2

from . import mapping
from .model import DropReason, MetricPoint
from .stats import BridgeStats

_CUMULATIVE = metrics_pb2.AGGREGATION_TEMPORALITY_CUMULATIVE


def _any_value(v):
    kind = v.WhichOneof("value")
    if kind is None:
        return ""
    if kind in ("array_value", "kvlist_value"):
        return str(getattr(v, kind))
    return getattr(v, kind)


def _attrs(kvs: Iterable) -> Dict[str, object]:
    return {kv.key: _any_value(kv.value) for kv in kvs}


def _number(dp) -> float:
    return float(dp.as_int if dp.WhichOneof("value") == "as_int" else dp.as_double)


class DeltaTracker:
    """Cumulative -> delta. First sight emits nothing; a decrease is a reset
    (the new value is emitted as the delta since the restart)."""

    def __init__(self) -> None:
        self._last: Dict[tuple, float] = {}

    def delta(self, key: tuple, value: float) -> Optional[float]:
        prev = self._last.get(key)
        self._last[key] = value
        if prev is None:
            return None
        if value < prev:
            return value
        return value - prev


class OtlpDecoder:
    def __init__(
        self,
        stats: Optional[BridgeStats] = None,
        limiter: Optional[mapping.CardinalityLimiter] = None,
        clock: mapping.Clock = mapping.utcnow,
        tracker: Optional[DeltaTracker] = None,
        defaults: Optional[Dict[str, object]] = None,
    ) -> None:
        self.stats = stats or BridgeStats()
        self.limiter = limiter or mapping.CardinalityLimiter()
        self.clock = clock
        self.tracker = tracker or DeltaTracker()
        # Resource attributes used only when the sender omits them (e.g. cloud.provider).
        self.defaults = dict(defaults or {})

    def decode(self, payload: bytes) -> List[MetricPoint]:
        req = ExportMetricsServiceRequest()
        req.ParseFromString(payload)
        now = self.clock()
        out: List[MetricPoint] = []
        for rm in req.resource_metrics:
            res = {**self.defaults, **_attrs(rm.resource.attributes)}
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    self._metric(metric, res, now, out)
        return out

    # -- per metric type -------------------------------------------------
    def _metric(self, m, res, now, out) -> None:
        kind = m.WhichOneof("data")
        if kind == "gauge":
            for dp in m.gauge.data_points:
                self._emit(out, res, dp, m.name, _number(dp), m.unit, now)
        elif kind == "sum":
            cumulative = m.sum.aggregation_temporality == _CUMULATIVE
            for dp in m.sum.data_points:
                value = _number(dp)
                if cumulative and m.sum.is_monotonic:
                    value = self._cumulative(res, m.name, dp, value)
                    if value is None:
                        continue
                self._emit(out, res, dp, m.name, value, m.unit, now)
        elif kind == "histogram":
            cumulative = m.histogram.aggregation_temporality == _CUMULATIVE
            for dp in m.histogram.data_points:
                self._total(out, res, m, dp, f"{m.name}_count", float(dp.count), now, cumulative)
                if dp.HasField("sum"):
                    self._total(out, res, m, dp, f"{m.name}_sum", dp.sum, now, cumulative)
                if dp.HasField("min"):
                    self._emit(out, res, dp, f"{m.name}_min", dp.min, m.unit, now)
                if dp.HasField("max"):
                    self._emit(out, res, dp, f"{m.name}_max", dp.max, m.unit, now)
        elif kind == "exponential_histogram":
            cumulative = m.exponential_histogram.aggregation_temporality == _CUMULATIVE
            for dp in m.exponential_histogram.data_points:
                self._total(out, res, m, dp, f"{m.name}_count", float(dp.count), now, cumulative)
                if dp.HasField("sum"):
                    self._total(out, res, m, dp, f"{m.name}_sum", dp.sum, now, cumulative)
        elif kind == "summary":
            # OTLP summaries are always cumulative: count and sum are running totals.
            for dp in m.summary.data_points:
                self._total(out, res, m, dp, f"{m.name}_count", float(dp.count), now, True)
                self._total(out, res, m, dp, f"{m.name}_sum", dp.sum, now, True)
                for q in dp.quantile_values:
                    self._emit(out, res, dp, m.name, q.value, m.unit, now,
                               extra={"quantile": repr(q.quantile)})
        else:
            self.stats.drop(DropReason.unsupported_type)

    # -- cumulative helpers ----------------------------------------------
    def _cumulative(self, res, name, dp, value) -> Optional[float]:
        key = (tuple(sorted(res.items())), name, tuple(sorted(_attrs(dp.attributes).items())))
        return self.tracker.delta(key, value)

    def _total(self, out, res, m, dp, name, value, now, cumulative) -> None:
        """Emit a count/sum series; cumulative running totals become deltas."""
        if cumulative:
            value = self._cumulative(res, name, dp, value)
            if value is None:
                return
        self._emit(out, res, dp, name, value, m.unit, now)

    # -- shared pipeline -------------------------------------------------
    def _emit(self, out, res, dp, name, value, unit, now, extra=None) -> None:
        ts = (datetime.fromtimestamp(dp.time_unix_nano / 1e9, tz=timezone.utc)
              if dp.time_unix_nano else now)
        reason = mapping.check_timestamp(ts, now)
        if reason:
            self.stats.drop(reason)
            return
        ns = mapping.namespace_for_resource(res)
        mname = mapping.sanitize_metric_name(name)
        if not mapping.validate_namespace(ns) or not mapping.validate_metric_name(mname):
            self.stats.drop(DropReason.invalid_namespace)
            return
        pattrs = _attrs(dp.attributes)
        if extra:
            pattrs.update(extra)
        dims, trimmed = mapping.build_dimensions(res, pattrs)
        if trimmed:
            self.stats.drop(DropReason.too_many_dims)  # point kept, extra dims trimmed
        if not self.limiter.allow(ns, mname, dims):
            self.stats.drop(DropReason.cardinality)
            return
        out.append(MetricPoint(ns, mname, dims, ts, float(value), unit or None, None))
