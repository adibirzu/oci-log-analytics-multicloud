from datetime import datetime, timedelta, timezone

import pytest

from collectors.metrics_bridge import mapping
from collectors.metrics_bridge.model import DropReason

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("cloud,service,expected", [
    ("aws", "ec2", "custom_aws_ec2"),
    ("AWS", "My Service!!", "custom_aws_my_service"),
    ("azure", "oci_x", "custom_azure_oci_x"),
    ("gcp", "serüvice-中", "custom_gcp_ser_vice"),
    ("aws", "a---b___c", "custom_aws_a_b_c"),
])
def test_namespace_sanitization(cloud, service, expected):
    ns = mapping.sanitize_namespace(cloud, service)
    assert ns == expected
    assert mapping.validate_namespace(ns)


def test_namespace_validation_rejects_reserved_and_long():
    assert not mapping.validate_namespace("oci_foo")
    assert not mapping.validate_namespace("oracle_foo")
    assert not mapping.validate_namespace("")
    assert not mapping.validate_namespace("a" * 256)
    assert len(mapping.sanitize_namespace("aws", "x" * 400)) <= mapping.MAX_NAMESPACE_LEN


@pytest.mark.parametrize("raw,expected", [
    ("cpu.usage", "cpu.usage"), ("cpu usage/%", "cpu_usage"), ("a__b", "a_b"), ("m-1", "m-1"),
])
def test_metric_name(raw, expected):
    out = mapping.sanitize_metric_name(raw)
    assert out == expected and mapping.validate_metric_name(out)


def test_dimensions_resource_hash_and_truncation():
    res = {"cloud.provider": "aws", "cloud.resource_id": "arn:x", "service.name": "s"}
    dims, trimmed = mapping.build_dimensions(res, {"k": "v" * 500, "n": 3})
    d = dict(dims)
    assert not trimmed
    assert len(d["cloud.resource_id"]) == 12 and d["cloud.resource_id"] != "arn:x"
    assert len(d["k"]) == 256 and d["n"] == "3"


def test_dimensions_over_20_trimmed_by_priority():
    res = {k: "v" for k in mapping.RESOURCE_DIM_KEYS}
    attrs = {f"p{i:02d}": "x" for i in range(30)}
    dims, trimmed = mapping.build_dimensions(res, attrs)
    d = dict(dims)
    assert trimmed and len(d) == 20
    assert all(k in d for k in mapping.RESOURCE_DIM_KEYS)
    assert "p14" in d and "p15" not in d  # sorted point attrs, first 15 kept


def test_dimensions_minimum_one():
    dims, _ = mapping.build_dimensions({}, {})
    assert dims == (("cloud.provider", "unknown"),)


@pytest.mark.parametrize("delta,expected", [
    (timedelta(hours=-2, seconds=-1), DropReason.stale),
    (timedelta(hours=-1), None),
    (timedelta(minutes=10, seconds=1), DropReason.future),
    (timedelta(minutes=9), None),
])
def test_timestamp_window(delta, expected):
    assert mapping.check_timestamp(NOW + delta, NOW) == expected


def test_cardinality_limiter():
    lim = mapping.CardinalityLimiter(2)
    assert lim.allow("ns", "a", ()) and lim.allow("ns", "b", ())
    assert not lim.allow("ns", "c", ())
    assert lim.allow("ns", "a", ())  # known stream still allowed
    assert lim.allow("other", "c", ())  # per namespace
    assert lim.dropped == {"ns": 1}
    lim.reset()
    assert lim.allow("ns", "c", ())
