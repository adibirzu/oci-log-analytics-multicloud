"""Offline Azure transport contracts; SDKs and provider credentials are optional."""

import json
import importlib.util
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from collectors.logs.sources.azure_eventhub import (
    AzureCollector, AzureCollectorConfig, build_azure_runtime, normalize_azure_record, receive_azure,
)

FIXTURES = Path(__file__).parent / "fixtures" / "azure"


def record(name="activity"):
    return json.loads((FIXTURES / f"{name}.json").read_text())["records"][0]


def test_activity_normalization():
    key, row = normalize_azure_record(record())
    assert key == "azure.activity"
    assert row["cloud"]["provider"] == "azure"
    assert row["cloud"]["account"]["uid"] == "11111111-1111-4111-8111-111111111111"
    assert row["actor"]["user"]["name"] == "operator@example.invalid"
    assert row["src_endpoint"]["ip"] == "192.0.2.10"
    assert row["activity_name"] == "Microsoft.Compute/virtualMachines/write"
    assert row["status"] == "Success"
    assert row["resource"].endswith("/virtualMachines/sample")
    assert row["timestamp"] == "2026-01-01T00:00:00Z"
    assert "claims" not in json.dumps(row)


class Event:
    def __init__(self, rows=None, sequence=1, body=None):
        self.sequence_number, self.offset = sequence, str(sequence)
        self.body = body if body is not None else json.dumps({"records": rows or [record()]})

    def body_as_str(self, encoding):
        return self.body


class Partition:
    partition_id = "0"

    def __init__(self, calls):
        self.calls = calls

    def update_checkpoint(self, event):
        self.calls.append(("checkpoint", event.sequence_number))


class Logan:
    def __init__(self, calls, fail_at=None, status=200):
        self.calls, self.fail_at, self.status = calls, fail_at, status
        self.uploads = []

    def get_log_analytics_log_group(self, *args):
        return SimpleNamespace(data=SimpleNamespace(compartment_id="sample-compartment"))

    def list_sources(self, *args, **kwargs):
        return SimpleNamespace(data=SimpleNamespace(items=[
            SimpleNamespace(display_name=name, name="generated-" + key)
            for key, name in self.names.items()
        ]), headers={})

    def upload_log_file(self, **kwargs):
        self.uploads.append({**kwargs, "upload_log_file_body": kwargs["upload_log_file_body"].read()})
        if len(self.uploads) == self.fail_at:
            raise RuntimeError("synthetic provider error")
        self.calls.append(("upload", kwargs["log_source_name"]))
        return SimpleNamespace(status=self.status)


def pipeline(tmp_path, **kwargs):
    cfg = AzureCollectorConfig(
        namespace="sample-namespace", log_group_ocid="ocid" + "1.loganalyticsloggroup.oc1..example",
        subscription_id="11111111-1111-4111-8111-111111111111",
        tenant_id="22222222-2222-4222-8222-222222222222",
        fully_qualified_namespace="sample.servicebus.windows.net", eventhub_name="sample-hub",
        consumer_group="sample-collector", dlq_directory=str(tmp_path), **kwargs.pop("config", {}),
    )
    calls = []
    logan = Logan(calls, **kwargs)
    logan.names = cfg.source_names
    collector = AzureCollector(cfg, logan=logan)
    return collector, calls, Partition(calls), logan


def test_checkpoint_after_all_uploads(tmp_path):
    collector, calls, partition, logan = pipeline(tmp_path, config={"max_batch_records": 1})
    assert collector.process_batch(partition, [Event([record(), record()])]) == "uploaded"
    assert [call[0] for call in calls] == ["upload", "upload", "checkpoint"]
    assert all(call["opc_meta_loggrpid"] == collector.config.log_group_ocid for call in logan.uploads)


@pytest.mark.parametrize("fail_at", [1, 2])
def test_failure_blocks_later_checkpoints(tmp_path, fail_at):
    collector, calls, partition, logan = pipeline(tmp_path, fail_at=fail_at, config={"max_batch_records": 1})
    event = Event([record(), record()])
    assert collector.process_batch(partition, [event, Event(sequence=2)]) == "retry"
    assert not any(call[0] == "checkpoint" for call in calls)
    assert collector.process_batch(partition, [Event(sequence=3)]) == "retry"
    restarted = AzureCollector(collector.config, logan=logan)
    assert restarted.process_batch(partition, [event]) == "uploaded"
    assert calls[-1] == ("checkpoint", 1)
    assert logan.uploads[0]["opc_retry_token"] == logan.uploads[fail_at]["opc_retry_token"]


@pytest.mark.parametrize("status", [None, 302, 400, 500, True])
def test_unaccepted_upload_never_checkpoints_or_deadletters(tmp_path, status):
    collector, calls, partition, _ = pipeline(tmp_path, status=status)
    assert collector.process_batch(partition, [Event()]) == "retry"
    assert not any(call[0] == "checkpoint" for call in calls)
    assert not list(tmp_path.glob("*.json"))


def test_poison_attempts_survive_restart_and_dlq_is_sanitized(tmp_path, caplog):
    collector, calls, partition, logan = pipeline(tmp_path, config={"max_receive_count": 2})
    event = Event(body="SYNTHETIC_SE" + "CRET invalid JSON")
    assert collector.process_batch(partition, [event]) == "retry"
    assert not calls
    restarted = AzureCollector(collector.config, logan=logan)
    assert restarted.process_batch(partition, [event]) == "dead_lettered"
    assert calls == [("checkpoint", 1)]
    envelope = json.loads(next(tmp_path.glob("*.dlq.json")).read_text())
    assert envelope["receive_count"] == 2
    assert envelope["sequence_number"] == 1
    assert all("SYNTHETIC" not in path.read_text() for path in tmp_path.glob("*.json"))
    assert "SYNTHETIC" not in caplog.text
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in tmp_path.glob("*.json"))


def test_failed_dlq_transfer_never_checkpoints(tmp_path):
    collector, calls, partition, _ = pipeline(tmp_path, config={"max_receive_count": 1})
    collector.poison_store.send = lambda *args: (_ for _ in ()).throw(OSError("synthetic secret"))
    assert collector.process_batch(partition, [Event(body="invalid")]) == "retry"
    assert not calls


@pytest.mark.parametrize("case", ["wrong_subscription", "wrong_tenant", "invalid_time", "unsupported_category", "late_bad_record", "oversize"])
def test_invalid_messages_are_validated_before_any_upload(tmp_path, case):
    collector, calls, partition, _ = pipeline(tmp_path)
    row = record("entra_signin" if case == "wrong_tenant" else "activity")
    if case == "wrong_subscription":
        row["subscriptionId"] = "99999999-9999-4999-8999-999999999999"
    elif case == "wrong_tenant":
        row["tenantId"] = "99999999-9999-4999-8999-999999999999"
    elif case == "invalid_time":
        row["time"] = "2026-01-01"
    elif case == "unsupported_category":
        row["category"] = "kube-audit"
    elif case == "oversize":
        collector.config = replace(collector.config, max_message_bytes=10)
    rows = [record(), {}] if case == "late_bad_record" else [row]
    assert collector.process_batch(partition, [Event(rows)]) == "retry"
    assert not calls


@pytest.mark.parametrize("name,key,ip", [("entra_signin", "azure.entra_signin", "198.51.100.20"),
                                       ("entra_audit", "azure.entra_audit", "203.0.113.30")])
def test_entra_normalization(name, key, ip):
    source, row = normalize_azure_record(record(name))
    assert source == key
    assert row["cloud"]["account"]["uid"] == "22222222-2222-4222-8222-222222222222"
    assert row["src_endpoint"]["ip"] == ip
    assert row["actor"]["user"]["name"] == "operator@example.invalid"
    assert row["status"] == ("Failure" if name == "entra_signin" else "Success")
    assert "untrusted_payload" not in json.dumps(row)


def test_mixed_categories_use_separate_catalog_sources(tmp_path):
    collector, calls, partition, logan = pipeline(tmp_path)
    rows = [record(name) for name in ("activity", "entra_signin", "entra_audit")]
    assert collector.process_batch(partition, [Event(rows)]) == "uploaded"
    assert [call["log_source_name"] for call in logan.uploads] == [
        "generated-azure.activity", "generated-azure.entra_signin", "generated-azure.entra_audit"]
    assert calls[-1] == ("checkpoint", 1)


def test_unknown_activity_status_and_hostname_are_not_success_or_ip():
    row = record()
    row["resultType"], row["callerIpAddress"] = "Started", "service.example.invalid"
    _, output = normalize_azure_record(row)
    assert output["status"] == "Unknown"
    assert output["src_endpoint"]["ip"] is None


def test_empty_batch_does_not_checkpoint(tmp_path):
    collector, calls, partition, _ = pipeline(tmp_path)
    assert collector.process_batch(partition, []) == "ignored"
    assert not calls


def test_receiver_stops_after_failure_even_if_sdk_delivers_again(tmp_path):
    adapter, calls, partition, _ = pipeline(tmp_path, fail_at=1)

    class Consumer:
        closed = False

        def receive_batch(self, **kwargs):
            assert kwargs["starting_position"] == "-1"
            kwargs["on_event_batch"](partition, [Event()])
            kwargs["on_event_batch"](partition, [Event(sequence=2)])

        def close(self):
            self.closed = True

    consumer = Consumer()
    assert receive_azure(adapter, consumer) == 1
    assert consumer.closed
    assert not any(call[0] == "checkpoint" for call in calls)


def test_catalog_content_and_cross_cloud_queries_agree():
    from collectors.logs.catalog import load_catalog
    from scripts.upsert_logan_content import load_content
    build_cross_cloud_queries = pytest.importorskip("datasources.cross_cloud_query", reason="optional datasources.cross_cloud_query is not part of this repo").build_cross_cloud_queries

    catalog = load_catalog()
    keys = ["azure.activity", "azure.entra_signin", "azure.entra_audit"]
    for key in keys:
        bundle = key.replace(".", "_") + "_ocsf"
        assets = load_content(bundle=bundle)
        source = next(item for item in assets if item.kind == "source")
        parser = next(item for item in assets if item.kind == "parser")
        fields = {item.display_name: item.name for item in assets if item.kind == "field"}
        assert catalog.status(key) == "implemented"
        assert source.display_name == catalog.source_name(key)
        assert source.body["parsers"] == [parser.name]
        for normalized, label in catalog.sources[key]["fields"].items():
            if normalized != "log.source":
                assert any(mapping["structured_column_info"] == "$." + normalized
                           and mapping["storage_field_name"] == fields[label] for mapping in parser.body["field_maps"])
    result = build_cross_cloud_queries(keys, providers=["azure"], filters={"ip": "192.0.2.10"})
    assert len(result.queries) == 3 and not result.skipped_sources
    assert all('"Azure Cloud Provider" = \'azure\'' in item["query"] for item in result.queries)


def test_runtime_rejects_incomplete_identity_without_sdk(tmp_path):
    adapter, _, _, _ = pipeline(tmp_path)
    with pytest.raises(ValueError):
        build_azure_runtime(adapter.config, {})


def azure_sdk_available():
    try:
        return all(importlib.util.find_spec(name) is not None for name in (
            "azure.identity", "azure.eventhub", "azure.eventhub.extensions.checkpointstoreblob"))
    except ModuleNotFoundError:
        return False


@pytest.mark.skipif(not azure_sdk_available(), reason="Azure SDKs are optional collector dependencies")
def test_workload_identity_sdk_construction_is_offline(tmp_path, monkeypatch):
    import azure.identity
    import azure.eventhub
    import azure.eventhub.extensions.checkpointstoreblob as checkpoint
    from collectors.common import oci_auth

    adapter, _, _, logan = pipeline(tmp_path)
    captured = {}

    def credential(**kwargs):
        captured["credential"] = kwargs
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(azure.identity, "WorkloadIdentityCredential", credential)
    monkeypatch.setattr(azure.eventhub, "EventHubConsumerClient", lambda **kwargs: captured.setdefault("consumer", kwargs))
    monkeypatch.setattr(checkpoint, "BlobCheckpointStore", lambda **kwargs: captured.setdefault("checkpoint", kwargs))
    monkeypatch.setattr(oci_auth, "build_oci_client", lambda *args, **kwargs: logan)
    env = {"AZURE_CLIENT_ID": "77777777-7777-4777-8777-777777777777", "AZURE_FEDERATED_TOKEN_FILE": "/unused/projected-token",
           "AZURE_CHECKPOINT_ACCOUNT_URL": "https://samplecheckpoint.blob.core.windows.net", "AZURE_CHECKPOINT_CONTAINER": "checkpoints"}
    built, _, _ = build_azure_runtime(adapter.config, env)
    assert isinstance(built, AzureCollector)
    assert captured["credential"]["token_file_path"] == env["AZURE_FEDERATED_TOKEN_FILE"]
    assert captured["consumer"]["checkpoint_store"] == captured["checkpoint"]


def test_exported_write_category_and_subscription_root_are_supported():
    row = record()
    row["category"] = "Write"
    row["resourceId"] = "/subscriptions/11111111-1111-4111-8111-111111111111"
    row["properties"]["resourceLocation"] = "sample-region"
    row["location"] = "global"
    key, result = normalize_azure_record(row)
    assert key == "azure.activity"
    assert result["cloud"]["region"] == "sample-region"


def test_invalid_utf8_is_poison_not_a_provider_outage(tmp_path):
    collector, calls, partition, _ = pipeline(tmp_path, config={"max_receive_count": 1})

    class InvalidEvent(Event):
        def body_as_str(self, encoding):
            return b"\xff".decode("utf-8")

    assert collector.process_batch(partition, [InvalidEvent()]) == "dead_lettered"
    assert calls == [("checkpoint", 1)]


def test_failed_checkpoint_replays_upload_with_stable_identity(tmp_path):
    collector, _, partition, logan = pipeline(tmp_path)
    partition.update_checkpoint = lambda event: (_ for _ in ()).throw(OSError("synthetic error"))
    assert collector.process_batch(partition, [Event()]) == "retry"
    calls = []
    assert AzureCollector(collector.config, logan=logan).process_batch(Partition(calls), [Event()]) == "uploaded"
    assert calls == [("checkpoint", 1)]
    assert logan.uploads[0]["opc_retry_token"] == logan.uploads[1]["opc_retry_token"]


@pytest.mark.parametrize("key", ["AZURE_CLIENT_SECRET", "AZURE_EVENTHUB_CONNECTION_STRING", "AZURE_STORAGE_CONNECTION_STRING", "AZURE_STORAGE_KEY"])
def test_config_rejects_shared_credentials(key):
    with pytest.raises(ValueError, match="shared credentials prohibited"):
        AzureCollectorConfig.from_env({key: "synthetic credential"})


def test_azure_content_reuses_upsert_and_generated_names_offline():
    from tests.test_aws_cloudtrail_ingest import ContentClient
    from scripts.upsert_logan_content import apply_content, load_content, plan_content

    client = ContentClient()
    for name in ("activity", "entra_signin", "entra_audit"):
        assets = load_content(bundle=f"azure_{name}_ocsf")
        before = sum(call[0] == "upsert" for call in client.calls)
        plan_content(client, "sample", "sample-compartment", assets)
        assert sum(call[0] == "upsert" for call in client.calls) == before
        apply_content(client, "sample", "sample-compartment", assets)
        assert apply_content(client, "sample", "sample-compartment", assets)["counts"]["unchanged"] == len(assets)
    assert sum(name.startswith("generated-") for name in client.resources["field"]) == 13  # Shared Azure custom fields.
    assert len(client.resources["source"]) == len(client.resources["parser"]) == 3


def test_invalid_unicode_field_is_sanitized_poison(tmp_path):
    collector, calls, partition, _ = pipeline(tmp_path, config={"max_receive_count": 1})
    row = record()
    row["operationName"] = "\ud800"
    assert collector.process_batch(partition, [Event([row])]) == "dead_lettered"
    assert calls == [("checkpoint", 1)]


@pytest.mark.parametrize("code", [True, False, 0.0, "unknown", None])
def test_invalid_signin_outcomes_stay_unknown(code):
    row = record("entra_signin")
    row["properties"]["status"]["errorCode"] = code
    assert normalize_azure_record(row)[1]["status"] == "Unknown"


def test_conflicting_explicit_entra_tenants_are_rejected_before_upload(tmp_path):
    collector, calls, partition, _ = pipeline(tmp_path)
    row = record("entra_signin")
    row["properties"]["tenantId"] = "99999999-9999-4999-8999-999999999999"
    assert collector.process_batch(partition, [Event([row])]) == "retry"
    assert not calls
