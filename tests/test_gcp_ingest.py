"""Offline GCP pull contracts; no credentials or cloud requests."""

import importlib.util
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from collectors.logs.sources.gcp_pubsub import (
    GCPCollector, GCPCollectorConfig, build_gcp_runtime, normalize_gcp_record,
)

FIXTURES = Path(__file__).parent / "fixtures" / "gcp"


def record(name="audit"):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def message(row=None, raw=None):
    return SimpleNamespace(ack_id="sample-ack", delivery_attempt=1,
                           message=SimpleNamespace(message_id="sample-message", data=raw if raw is not None else json.dumps(row or record()).encode()))


class Subscriber:
    def __init__(self, calls):
        self.calls, self.response = calls, []
        self.fail_lease = self.fail_ack = False

    def modify_ack_deadline(self, request, **kwargs):
        self.calls.append(("lease", request))
        if self.fail_lease:
            raise RuntimeError("synthetic failure")

    def acknowledge(self, request, **kwargs):
        if self.fail_ack:
            raise RuntimeError("synthetic failure")
        self.calls.append(("ack", request))

    def pull(self, request, **kwargs):
        self.calls.append(("pull", request))
        return SimpleNamespace(received_messages=self.response)


class Publisher:
    def __init__(self, calls):
        self.calls, self.published, self.fail = calls, [], False

    def publish(self, topic, data):
        self.published.append((topic, data))
        self.calls.append(("dlq", topic))

        def result(timeout):
            if self.fail:
                raise RuntimeError("synthetic failure")
            return "sample-dlq-message"

        return SimpleNamespace(result=result)

    def stop(self):
        self.calls.append(("publisher_stop", None))


class Logan:
    def __init__(self, calls, names, status=200):
        self.calls, self.names, self.status, self.uploads = calls, names, status, []

    def get_log_analytics_log_group(self, *args):
        return SimpleNamespace(data=SimpleNamespace(compartment_id="sample-compartment"))

    def list_sources(self, *args, **kwargs):
        return SimpleNamespace(data=SimpleNamespace(items=[SimpleNamespace(display_name=value, name=key) for key, value in self.names.items()]), headers={})

    def upload_log_file(self, **kwargs):
        self.uploads.append({**kwargs, "upload_log_file_body": kwargs["upload_log_file_body"].read()})
        self.calls.append(("upload", kwargs["log_source_name"]))
        if isinstance(self.status, Exception):
            raise self.status
        return SimpleNamespace(status=self.status)


def pipeline(tmp_path, status=200, **config):
    values = dict(namespace="sample-namespace", log_group_ocid="ocid" + "1.loganalyticsloggroup.oc1..example",
                  subscription="projects/transport-project/subscriptions/sample-pull",
                  allowed_project_ids=frozenset({"sample-project"}), dlq_directory=str(tmp_path),
                  dlq_topic="projects/transport-project/topics/sample-dlq")
    cfg = GCPCollectorConfig(**{**values, **config})
    calls = []
    subscriber, logan = Subscriber(calls), Logan(calls, cfg.source_names, status)
    return GCPCollector(cfg, subscriber=subscriber, publisher=Publisher(calls), logan=logan), calls, subscriber, logan


@pytest.mark.parametrize("name,key,status", [("audit", "gcp.audit", "Success"), ("data_access", "gcp.audit", "Failure"),
                                          ("gke_audit", "gcp.gke_audit", "Success"), ("vpc_flow", "gcp.vpc_flow", "Unknown")])
def test_normalized_catalog_shape(name, key, status):
    source, row = normalize_gcp_record(record(name))
    assert source == key
    assert row["cloud"]["provider"] == "gcp"
    assert row["cloud"]["account"]["uid"] == "sample-project"
    assert row["status"] == status
    assert row["timestamp"] == "2026-01-01T00:00:00Z"
    if name != "vpc_flow":
        assert row["actor"]["user"]["name"] == "operator@example.com"
        assert row["src_endpoint"]["ip"] == "192.0.2.10"
        assert row["activity_name"] == record(name)["protoPayload"]["methodName"]
        assert row["resource"] == record(name)["protoPayload"]["resourceName"]
    else:
        assert row["src_endpoint"]["ip"] == "198.51.100.20"
        assert row["dst_endpoint"]["ip"] == "203.0.113.30"
    assert "drop-me" not in json.dumps(row)


@pytest.mark.parametrize("name", ["audit", "gke_audit", "vpc_flow"])
def test_upload_then_ack(tmp_path, name):
    adapter, calls, _, logan = pipeline(tmp_path)
    assert adapter.process_message(message(record(name))) == "uploaded"
    assert [call[0] for call in calls] == ["lease", "upload", "ack"]
    assert logan.uploads[0]["opc_meta_loggrpid"] == adapter.config.log_group_ocid


@pytest.mark.parametrize("status", [None, 302, 400, 500, True, RuntimeError("synthetic failure")])
def test_failed_upload_never_acks_or_deadletters(tmp_path, status):
    adapter, calls, _, _ = pipeline(tmp_path, status, max_receive_count=1)
    assert adapter.process_message(message()) == "retry"
    assert not any(call[0] == "ack" for call in calls)
    assert not list(tmp_path.glob("*.json"))


def test_failed_ack_replays_stable_upload(tmp_path):
    adapter, _, subscriber, logan = pipeline(tmp_path)
    subscriber.fail_ack = True
    assert adapter.process_message(message()) == "retry"
    subscriber.fail_ack = False
    assert adapter.process_message(message()) == "uploaded"
    assert logan.uploads[0]["opc_retry_token"] == logan.uploads[1]["opc_retry_token"]


def test_lost_lease_never_uploads_or_acks(tmp_path):
    adapter, calls, subscriber, _ = pipeline(tmp_path)
    subscriber.fail_lease = True
    assert adapter.process_message(message()) == "retry"
    assert [call[0] for call in calls] == ["lease"]


def test_durable_sanitized_poison_then_ack(tmp_path, caplog):
    adapter, calls, subscriber, logan = pipeline(tmp_path, max_receive_count=2)
    incoming = message(raw=b"SYNTHETIC_SE" + b"CRET not JSON")
    assert adapter.process_message(incoming) == "retry"
    restarted = GCPCollector(adapter.config, subscriber=subscriber, publisher=adapter.publisher, logan=logan)
    assert restarted.process_message(incoming) == "dead_lettered"
    assert [call[0] for call in calls] == ["lease", "lease", "dlq", "ack"]
    envelope = json.loads(next(tmp_path.glob("*.dlq.json")).read_text())
    assert envelope["receive_count"] == 2
    assert envelope["schema_version"] == "mcingest.poison.v1"
    assert "SYNTHETIC" not in caplog.text
    assert all(b"SYNTHETIC" not in data for _, data in adapter.publisher.published)
    assert all("SYNTHETIC" not in path.read_text() and path.stat().st_mode & 0o777 == 0o600 for path in tmp_path.glob("*.json"))


def test_failed_dlq_never_acks(tmp_path):
    adapter, calls, _, _ = pipeline(tmp_path, max_receive_count=1)
    adapter.poison_store.send = lambda *args: (_ for _ in ()).throw(OSError("synthetic failure"))
    assert adapter.process_message(message(raw=b"invalid")) == "retry"
    assert not any(call[0] == "ack" for call in calls)


@pytest.mark.parametrize("case", ["wrong_project", "conflicting_project", "missing_project", "invalid_time", "unsupported", "oversize", "invalid_utf8", "invalid_unicode", "not_object"])
def test_invalid_input_is_poison_before_upload(tmp_path, case):
    adapter, calls, _, _ = pipeline(tmp_path, max_receive_count=1)
    row = record()
    raw = None
    if case == "wrong_project":
        row["logName"] = row["logName"].replace("sample-project", "other-project")
        row["resource"]["labels"]["project_id"] = "other-project"
    elif case == "conflicting_project":
        row["resource"]["labels"]["project_id"] = "other-project"
    elif case == "missing_project":
        row.pop("logName")
        row["resource"]["labels"].pop("project_id")
    elif case == "invalid_time":
        row["timestamp"] = "2026-01-01"
    elif case == "unsupported":
        row["logName"] = "projects/sample-project/logs/unsupported"
    elif case == "oversize":
        adapter.config = replace(adapter.config, max_message_bytes=10)
    elif case == "invalid_utf8":
        raw = b"\xff"
    elif case == "invalid_unicode":
        row["protoPayload"]["methodName"] = "\ud800"
    elif case == "not_object":
        raw = b"[]"
    assert adapter.process_message(message(row, raw)) == "dead_lettered"
    assert not any(call[0] == "upload" for call in calls)


@pytest.mark.parametrize("code", [True, False, 0.0, "unknown", None, -1, 99])
def test_invalid_audit_status_is_unknown(code):
    row = record()
    row["protoPayload"]["status"] = {"code": code}
    assert normalize_gcp_record(row)[1]["status"] == "Unknown"


def test_redacted_caller_ip_is_not_ip():
    row = record()
    row["protoPayload"]["requestMetadata"]["callerIp"] = "private"
    assert normalize_gcp_record(row)[1]["src_endpoint"]["ip"] is None


def test_empty_pull_and_single_inflight_message(tmp_path):
    adapter, calls, subscriber, _ = pipeline(tmp_path)
    assert adapter.poll_once() == {"uploaded": 0, "retry": 0, "dead_lettered": 0}
    subscriber.response = [message()]
    assert adapter.poll_once()["uploaded"] == 1
    assert calls[0][1]["max_messages"] == 1


def test_config_runtime_fail_closed_without_sdk(tmp_path):
    adapter, _, _, _ = pipeline(tmp_path)
    with pytest.raises(ValueError):
        build_gcp_runtime(adapter.config, {})
    for key in ("GOOGLE_APPLICATION_CREDENTIALS", "GCP_SERVICE_ACCOUNT_KEY"):
        with pytest.raises(ValueError):
            GCPCollectorConfig.from_env({key: "synthetic"})


@pytest.mark.parametrize("change", [{"subscription": "bad"}, {"allowed_project_ids": frozenset()}, {"ack_deadline_seconds": 601}, {"dlq_directory": "relative"}])
def test_config_bounds(tmp_path, change):
    with pytest.raises(ValueError):
        pipeline(tmp_path, **change)


def test_catalog_content_and_cross_cloud_queries_agree():
    from collectors.logs.catalog import load_catalog
    from scripts.upsert_logan_content import load_content
    build_cross_cloud_queries = pytest.importorskip("datasources.cross_cloud_query", reason="optional datasources.cross_cloud_query is not part of this repo").build_cross_cloud_queries
    catalog = load_catalog()
    keys = ["gcp.audit", "gcp.vpc_flow", "gcp.gke_audit"]
    for key in keys:
        assets = load_content(bundle=key.replace(".", "_") + "_ocsf")
        source = next(item for item in assets if item.kind == "source")
        parser = next(item for item in assets if item.kind == "parser")
        fields = {item.display_name: item.name for item in assets if item.kind == "field"}
        assert catalog.status(key) == "implemented"
        assert source.display_name == catalog.source_name(key)
        assert source.body["parsers"] == [parser.name]
        for normalized, label in catalog.sources[key]["fields"].items():
            if normalized != "log.source":
                assert any(mapping["structured_column_info"] == "$." + normalized and mapping["storage_field_name"] == fields[label] for mapping in parser.body["field_maps"])
    result = build_cross_cloud_queries(keys, providers=["gcp"], filters={"ip": "192.0.2.10"})
    assert len(result.queries) == 3 and not result.skipped_sources
    assert all('"GCP Cloud Provider" = \'gcp\'' in item["query"] for item in result.queries)


def gcp_sdk_available():
    try:
        return all(importlib.util.find_spec(name) is not None for name in ("google.auth.identity_pool", "google.cloud.pubsub_v1"))
    except ModuleNotFoundError:
        return False


@pytest.mark.skipif(not gcp_sdk_available(), reason="Google SDKs are optional collector dependencies")
def test_sdk_construction_offline(tmp_path, monkeypatch):
    from google.auth import identity_pool
    from google.cloud import pubsub_v1
    from collectors.common import oci_auth
    adapter, _, subscriber, logan = pipeline(tmp_path)
    captured = {}
    monkeypatch.setattr(identity_pool.Credentials, "from_info", lambda info, **kwargs: captured.setdefault("credentials", info))
    monkeypatch.setattr(pubsub_v1, "PublisherClient", lambda **kwargs: adapter.publisher)
    monkeypatch.setattr(pubsub_v1, "SubscriberClient", lambda **kwargs: captured.setdefault("subscriber", kwargs) and subscriber)
    monkeypatch.setattr(oci_auth, "build_oci_client", lambda *args, **kwargs: logan)
    provider = "projects/123456789012/locations/global/workloadIdentityPools/sample-pool/providers/sample-provider"
    built = build_gcp_runtime(adapter.config, {"GCP_WIF_PROVIDER_RESOURCE": provider, "GCP_WIF_TOKEN_FILE": "/unused/projected-token"})
    assert isinstance(built, GCPCollector)
    assert captured["credentials"]["type"] == "external_account"
    assert captured["credentials"]["credential_source"] == {"file": "/unused/projected-token", "format": {"type": "text"}}
    assert "service_account_impersonation_url" not in captured["credentials"]


def test_content_upsert_reuses_shared_planner():
    from tests.test_aws_cloudtrail_ingest import ContentClient
    from scripts.upsert_logan_content import apply_content, load_content
    client = ContentClient()
    for name in ("audit", "vpc_flow", "gke_audit"):
        assets = load_content(bundle=f"gcp_{name}_ocsf")
        apply_content(client, "sample", "sample-compartment", assets)
        assert apply_content(client, "sample", "sample-compartment", assets)["counts"]["unchanged"] == len(assets)
    assert len(client.resources["source"]) == len(client.resources["parser"]) == 3


def test_failed_sanitized_pubsub_publish_never_acks(tmp_path):
    adapter, calls, _, _ = pipeline(tmp_path, max_receive_count=1)
    adapter.publisher.fail = True
    assert adapter.process_message(message(raw=b"invalid")) == "retry"
    assert not any(call[0] == "ack" for call in calls)
    adapter.publisher.fail = False
    assert adapter.process_message(message(raw=b"invalid")) == "dead_lettered"
    assert json.loads(adapter.publisher.published[-1][1])["reason"] == "invalid_message"


def test_repeated_upload_outages_do_not_enter_any_dlq(tmp_path):
    adapter, calls, _, _ = pipeline(tmp_path, status=500, max_receive_count=1)
    incoming = message()
    incoming.delivery_attempt = 100
    for _ in range(6):
        assert adapter.process_message(incoming) == "retry"
    assert not list(tmp_path.glob("*.json"))
    assert not any(call[0] in {"ack", "dlq"} for call in calls)


def test_asynchronous_lease_failure_during_upload_never_acks(tmp_path, monkeypatch):
    import threading
    import collectors.logs.sources.gcp_pubsub as gcp
    adapter, calls, subscriber, _ = pipeline(tmp_path)
    captured = {}

    class ControlledStop:
        def wait(self, timeout):
            return False

        def set(self):
            pass

    events = [ControlledStop(), threading.Event()]
    monkeypatch.setattr(gcp, "Event", lambda: events.pop(0))

    class ControlledThread:
        def __init__(self, target, **kwargs):
            captured["heartbeat"] = target

        def start(self):
            pass

        def join(self, **kwargs):
            pass

        def is_alive(self):
            return False

    monkeypatch.setattr(gcp, "Thread", ControlledThread)
    original = adapter.uploader.upload

    def upload(*args, **kwargs):
        original(*args, **kwargs)
        subscriber.fail_lease = True
        captured["heartbeat"]()

    adapter.uploader.upload = upload
    assert adapter.process_message(message()) == "retry"
    assert [call[0] for call in calls] == ["lease", "upload", "lease"]


def test_cli_gcp_once_uses_shared_poll_loop_and_closes_clients(tmp_path, monkeypatch):
    from collectors.logs.__main__ import main
    import collectors.logs.sources.gcp_pubsub as gcp
    adapter, calls, subscriber, _ = pipeline(tmp_path)
    subscriber.response = [message()]
    subscriber.close = lambda: calls.append(("subscriber_close", None))
    monkeypatch.setattr(gcp.GCPCollectorConfig, "from_env", lambda env=None: adapter.config)
    monkeypatch.setattr(gcp, "build_gcp_runtime", lambda cfg, env=None: adapter)
    assert main(["--source", "gcp", "--once"]) == 0
    assert [call[0] for call in calls] == ["pull", "lease", "upload", "ack", "subscriber_close", "publisher_stop"]


def test_explicit_null_audit_status_stays_unknown():
    row = record()
    row["protoPayload"]["status"] = None
    assert normalize_gcp_record(row)[1]["status"] == "Unknown"


def test_networkmanagement_vpc_flow_log_id_is_supported():
    row = record("vpc_flow")
    row["logName"] = row["logName"].replace("compute.googleapis.com", "networkmanagement.googleapis.com")
    assert normalize_gcp_record(row)[0] == "gcp.vpc_flow"


def test_terraform_keeps_scoped_identity_and_sanitized_dlq_boundary():
    root = Path(__file__).resolve().parents[1]
    main = (root / "modules/logs/gcp/terraform/main.tf").read_text()
    assert "dead_letter_policy" not in main  # Native forwarding would copy raw bodies during OCI outages.
    assert 'resource "google_pubsub_topic" "dead_letter"' in main
    assert 'resource "google_pubsub_subscription" "dead_letter"' in main
    assert 'unique_writer_identity = true' in main
    assert 'self.writer_identity != "serviceAccount:cloud-logs@system.gserviceaccount.com"' in main
    assert 'resource "google_project_iam' not in main
    assert 'resource "google_service_account_key"' not in main
    subscriber = main.split('resource "google_pubsub_subscription_iam_member" "collector"')[1]
    assert 'subscription = google_pubsub_subscription.collector.name' in subscriber
    assert 'role         = "roles/pubsub.subscriber"' in subscriber
    publisher = main.split('resource "google_pubsub_topic_iam_member" "collector_dlq"')[1]
    assert 'topic   = google_pubsub_topic.dead_letter.name' in publisher
    assert 'role    = "roles/pubsub.publisher"' in publisher
