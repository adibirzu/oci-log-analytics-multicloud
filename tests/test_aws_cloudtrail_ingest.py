"""Hermetic AWS ingestion contracts: no credentials or cloud calls."""

import gzip
import importlib.util
import io
import json
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from collectors.logs import Collector, CollectorConfig, InvalidMessage, batch_ndjson, normalize_record
from collectors.logs.catalog import load_catalog

FIXTURE = Path(__file__).parent / "fixtures" / "aws" / "cloudtrail.json"
KEY = "cloudtrail/AWSLogs/123456789012/CloudTrail/us-east-1/2026/01/01/123456789012_CloudTrail_us-east-1_20260101T0000Z_example.json.gz"


def records():
    return json.loads(FIXTURE.read_text())["Records"]


def config(**kwargs):
    return CollectorConfig(
        namespace="sample-namespace", log_group_ocid="ocid1.loganalyticsloggroup.oc1..example",
        source_name="AWS CloudTrail (OCSF)", queue_url="https://sqs.us-east-1.amazonaws.com/123456789012/sample",
        dlq_url="https://sqs.us-east-1.amazonaws.com/123456789012/sample-dlq",
        bucket="sample-cloudtrail", prefix="cloudtrail", account_id="123456789012",
        aws_region="us-east-1", aws_role_arn="arn:aws:iam::123456789012:role/sample-collector",
        web_identity_token_file="/unused/projected-token", **kwargs,
    )


def message(key=KEY, count=1, bucket="sample-cloudtrail"):
    body = {"Records": [{"eventSource": "aws:s3", "eventName": "ObjectCreated:Put", "awsRegion": "us-east-1",
                         "s3": {"bucket": {"name": bucket}, "object": {"key": key}}}]}
    return {"Body": json.dumps(body), "ReceiptHandle": "sample-receipt", "MessageId": "sample-message",
            "Attributes": {"ApproximateReceiveCount": str(count)}}


class Queue:
    def __init__(self, events, messages=None, fail_send=False, fail_delete=False):
        self.events = events
        self.messages = messages or []
        self.fail_send = fail_send
        self.fail_delete = fail_delete
        self.sent = []

    def receive_message(self, **kwargs):
        self.receive_kwargs = kwargs
        return {"Messages": self.messages}

    def delete_message(self, **kwargs):
        if self.fail_delete:
            raise RuntimeError("delete failed")
        self.events.append("delete")

    def send_message(self, **kwargs):
        if self.fail_send:
            raise RuntimeError("DLQ failed")
        self.events.append("dlq")
        self.sent.append(kwargs)
        return {"MessageId": "sample-dlq-message"}

    def change_message_visibility(self, **kwargs):
        self.events.append("visibility")


class Objects:
    def __init__(self, data=None):
        self.data = data if data is not None else gzip.compress(FIXTURE.read_bytes())
        self.calls = []

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        return {"Body": io.BytesIO(self.data), "ContentLength": len(self.data)}


class Logan:
    def __init__(self, events, fail_at=None, status=200):
        self.events, self.fail_at, self.status = events, fail_at, status
        self.uploads = []

    def get_log_analytics_log_group(self, namespace, group):
        return SimpleNamespace(data=SimpleNamespace(compartment_id="sample-compartment"))

    def list_sources(self, namespace, compartment, **kwargs):
        source = SimpleNamespace(display_name="AWS CloudTrail (OCSF)", name="generated-source-internal")
        return SimpleNamespace(data=SimpleNamespace(items=[source]), headers={})

    def upload_log_file(self, **kwargs):
        self.uploads.append({**kwargs, "upload_log_file_body": kwargs["upload_log_file_body"].read()})
        if len(self.uploads) == self.fail_at:
            raise RuntimeError("upload failed")
        self.events.append("upload")
        return SimpleNamespace(status=self.status, data=SimpleNamespace(reference="sample-upload"))


def pipeline(cfg=None, **kwargs):
    events = []
    sqs = Queue(events, **{k: v for k, v in kwargs.items() if k in {"messages", "fail_send", "fail_delete"}})
    s3 = Objects(kwargs.get("data"))
    logan = Logan(events, kwargs.get("fail_at"), kwargs.get("status", 200))
    return Collector(cfg or config(), sqs=sqs, s3=s3, logan=logan), events, sqs, s3, logan


def test_normalization_and_catalog():
    normalized = [normalize_record(x) for x in records()]
    assert normalized[0]["cloud"] == {"provider": "aws", "account": {"uid": "123456789012"}, "region": "us-east-1"}
    assert normalized[0]["actor"]["user"]["name"] == "sample-operator"
    assert normalized[0]["src_endpoint"]["ip"] == "192.0.2.10"
    assert normalized[0]["activity_name"] == "DescribeInstances"
    assert normalized[0]["status"] == "Success"
    assert normalized[0]["resource"].endswith("instance/i-example")
    assert normalized[1]["actor"]["user"]["name"] == "sample-role"
    assert normalized[1]["status"] == "Failure"
    assert normalized[2]["src_endpoint"]["ip"] is None
    assert normalized[2]["source_address"] == "lambda.amazonaws.com"
    assert load_catalog().source_name("aws.cloudtrail") == "AWS CloudTrail (OCSF)"
    assert load_catalog().status("aws.cloudtrail") == "implemented"


@pytest.mark.parametrize("field", ["eventTime", "eventName", "eventID", "recipientAccountId", "awsRegion"])
def test_missing_required_record_field_is_poison(field):
    row = records()[0]
    row.pop(field)
    with pytest.raises(InvalidMessage):
        normalize_record(row)


def test_sensitive_raw_fields_are_not_forwarded():
    row = records()[0]
    row["userIdentity"]["accessKeyId"] = "SYNTHETIC_ACC" + "ESS_KEY_SECRET"
    row["requestParameters"] = {"password": "SYNTHETIC_PA" + "SSWORD_SECRET"}
    row["responseElements"] = {"token": "SYNTHETIC_T" + "OKEN_SECRET"}
    assert "SYNTHETIC" not in json.dumps(normalize_record(row))


def test_count_and_byte_batch_limits():
    rows = [normalize_record(x) for x in records()]
    batches = list(batch_ndjson(rows, max_records=2, max_bytes=4096))
    assert [len(x.splitlines()) for x in batches] == [2, 1]
    assert all(x.endswith(b"\n") for x in batches)
    assert [json.loads(line) for batch in batches for line in batch.splitlines()] == rows
    size = max(len(json.dumps(row, separators=(",", ":"), ensure_ascii=False).encode()) + 1 for row in rows)
    assert all(len(batch) <= size for batch in batch_ndjson(rows, max_records=10, max_bytes=size))
    with pytest.raises(InvalidMessage):
        list(batch_ndjson(rows, max_records=10, max_bytes=5))


def test_ack_after_all_batches_with_required_group_and_source():
    collector, events, _, _, logan = pipeline(config(max_batch_records=1))
    assert collector.process_message(message()) == "uploaded"
    assert events.count("upload") == 3
    assert events[-1] == "delete"
    for call in logan.uploads:
        assert call["opc_meta_loggrpid"] == collector.config.log_group_ocid
        assert call["log_source_name"] == "generated-source-internal"
        assert call["namespace_name"] == collector.config.namespace


@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_failed_upload_leaves_message_unacked(fail_at):
    collector, events, _, _, _ = pipeline(config(max_batch_records=1), fail_at=fail_at)
    assert collector.process_message(message(count=5)) == "retry"
    assert "delete" not in events
    assert "dlq" not in events


@pytest.mark.parametrize("status", [None, 302, 400, 500])
def test_non_success_response_is_not_acked(status):
    collector, events, _, _, _ = pipeline(status=status)
    assert collector.process_message(message()) == "retry"
    assert "delete" not in events


def test_repeat_upload_uses_stable_retry_token_and_filename():
    collector, _, _, _, logan = pipeline()
    collector.process_message(message())
    collector.process_message(message())
    assert logan.uploads[0]["opc_retry_token"] == logan.uploads[1]["opc_retry_token"]
    assert logan.uploads[0]["filename"] == logan.uploads[1]["filename"]


def test_poison_retries_then_sanitized_dlq_before_ack():
    collector, events, sqs, _, _ = pipeline()
    poison = message()
    poison["Body"] = "SYNTHETIC_PASSWORD_SECRET malformed body"
    assert collector.process_message(poison) == "retry"
    poison["Attributes"]["ApproximateReceiveCount"] = "5"
    assert collector.process_message(poison) == "dead_lettered"
    assert events[-2:] == ["dlq", "delete"]
    assert "SYNTHETIC_PASSWORD_SECRET" not in sqs.sent[0]["MessageBody"]


def test_failed_dlq_send_never_deletes_original():
    collector, events, _, _, _ = pipeline(fail_send=True, data=b"not gzip")
    assert collector.process_message(message(count=5)) == "retry"
    assert "delete" not in events


@pytest.mark.parametrize("kwargs", [{"bucket": "other-bucket"}, {"key": "outside/sample.json.gz"},
                                    {"key": "cloudtrail/AWSLogs/999999999999/CloudTrail/sample.json.gz"}])
def test_untrusted_object_reference_is_never_read(kwargs):
    collector, _, _, s3, logan = pipeline()
    assert collector.process_message(message(**kwargs)) == "retry"
    assert not s3.calls and not logan.uploads


def test_s3_test_event_is_ignored_and_acked():
    collector, events, _, s3, _ = pipeline()
    test_message = message()
    test_message["Body"] = json.dumps({"Service": "Amazon S3", "Event": "s3:TestEvent", "Bucket": "sample-cloudtrail"})
    assert collector.process_message(test_message) == "ignored"
    assert events[-1] == "delete" and "upload" not in events and not s3.calls


def test_digest_notification_is_ignored():
    collector, events, _, s3, _ = pipeline()
    digest = KEY.replace("/CloudTrail/", "/CloudTrail-Digest/")
    assert collector.process_message(message(key=digest)) == "ignored"
    assert events[-1] == "delete" and "upload" not in events and not s3.calls


def test_gzip_decompression_is_bounded():
    collector, events, _, _, _ = pipeline(config(max_object_bytes=100), data=gzip.compress(b"x" * 101))
    assert collector.process_message(message()) == "retry"
    assert "delete" not in events


def test_invalid_late_record_does_not_partially_upload():
    row = records()
    row[-1]["eventTime"] = "invalid"
    collector, events, _, _, logan = pipeline(config(max_batch_records=1), data=gzip.compress(json.dumps({"Records": row}).encode()))
    assert collector.process_message(message()) == "retry"
    assert not logan.uploads and "delete" not in events


def test_no_credentials_or_payloads_in_logs(caplog):
    collector, _, _, _, logan = pipeline()
    secrets = ["SYNTHETIC_ACCESS_KEY_SECRET", "SYNTHETIC_TOKEN_SECRET", "SYNTHETIC_PASSWORD_SECRET"]

    def leak(**kwargs):
        raise RuntimeError(" ".join(secrets))

    logan.upload_log_file = leak
    with caplog.at_level(logging.DEBUG):
        assert collector.process_message(message(count=5)) == "retry"
        malformed = message(count=5)
        malformed["Body"] = " ".join(secrets)
        collector.process_message(malformed)
    assert all(secret not in caplog.text for secret in secrets)
    assert "sample-receipt" not in caplog.text


def test_poll_requests_receive_count():
    collector, events, sqs, _, _ = pipeline(messages=[message()])
    assert collector.poll_once()["uploaded"] == 1
    assert "ApproximateReceiveCount" in sqs.receive_kwargs["MessageSystemAttributeNames"]
    assert events[-1] == "delete"


def test_env_config_requires_group_and_no_static_keys():
    env = {"LOGAN_NAMESPACE": "sample", "LOGAN_LOG_GROUP_OCID": "ocid1.loganalyticsloggroup.oc1..example",
           "AWS_SQS_QUEUE_URL": config().queue_url, "AWS_SQS_DLQ_URL": config().dlq_url,
           "AWS_S3_BUCKET": "sample-cloudtrail", "AWS_S3_PREFIX": "cloudtrail", "AWS_ACCOUNT_ID": "123456789012",
           "AWS_REGION": "us-east-1", "AWS_ROLE_ARN": config().aws_role_arn, "AWS_WEB_IDENTITY_TOKEN_FILE": "/projected/token"}
    assert CollectorConfig.from_env(env).source_name == load_catalog().source_name("aws.cloudtrail")
    with pytest.raises(ValueError):
        CollectorConfig.from_env({k: v for k, v in env.items() if k != "LOGAN_LOG_GROUP_OCID"})
    with pytest.raises(ValueError):
        CollectorConfig.from_env({**env, "AWS_ACCESS_KEY_ID": "SYNTHETIC_ACCESS_KEY_SECRET"})


def test_operator_api_key_auth_requires_explicit_opt_in():
    with pytest.raises(ValueError):
        config(oci_auth_mode="api_key")
    assert config(oci_auth_mode="api_key", allow_operator_auth=True).oci_auth_mode == "api_key"
    env = {"LOGAN_NAMESPACE": "sample", "LOGAN_LOG_GROUP_OCID": "ocid1.loganalyticsloggroup.oc1..example",
           "AWS_SQS_QUEUE_URL": config().queue_url, "AWS_SQS_DLQ_URL": config().dlq_url,
           "AWS_S3_BUCKET": "sample-cloudtrail", "AWS_S3_PREFIX": "cloudtrail", "AWS_ACCOUNT_ID": "123456789012",
           "AWS_REGION": "us-east-1", "AWS_ROLE_ARN": config().aws_role_arn, "AWS_WEB_IDENTITY_TOKEN_FILE": "/projected/token",
           "OCI_AUTH_MODE": "api_key"}
    with pytest.raises(ValueError):
        CollectorConfig.from_env(env)
    with pytest.raises(ValueError):
        CollectorConfig.from_env({**env, "INGEST_ALLOW_OPERATOR_AUTH": "yes"})
    assert CollectorConfig.from_env({**env, "INGEST_ALLOW_OPERATOR_AUTH": "true"}).allow_operator_auth


def test_invalid_config_boundaries():
    for changes in [{"queue_url": config().dlq_url}, {"max_batch_records": 0}, {"account_id": "bad"},
                    {"aws_auth_mode": "external_id", "external_id": ""}, {"prefix": "../bad"}]:
        with pytest.raises(ValueError):
            replace(config(), **changes)


def test_multi_object_failure_keeps_whole_message_and_uploads_nothing():
    collector, events, _, s3, logan = pipeline()
    original = s3.get_object

    def fetch(**kwargs):
        if kwargs["Key"].endswith("second.json.gz"):
            raise RuntimeError("S3 unavailable")
        return original(**kwargs)

    s3.get_object = fetch
    notification = message()
    body = json.loads(notification["Body"])
    second = json.loads(message(key=KEY.replace("example.json.gz", "second.json.gz"))["Body"])["Records"][0]
    body["Records"].append(second)
    notification["Body"] = json.dumps(body)
    assert collector.process_message(notification) == "retry"
    assert not logan.uploads and "delete" not in events


def test_failed_ack_retries_with_stable_upload_token():
    collector, events, sqs, _, logan = pipeline(fail_delete=True)
    assert collector.process_message(message()) == "retry"
    sqs.fail_delete = False
    assert collector.process_message(message(count=2)) == "uploaded"
    assert logan.uploads[0]["opc_retry_token"] == logan.uploads[1]["opc_retry_token"]
    assert events[-1] == "delete"


def test_sdk_debug_logging_is_suppressed():
    from collectors.logs.runtime import suppress_sdk_payload_logging

    sdk = logging.getLogger("botocore.auth")
    previous = sdk.level
    try:
        sdk.setLevel(logging.DEBUG)
        suppress_sdk_payload_logging()
        assert not sdk.isEnabledFor(logging.DEBUG)
    finally:
        sdk.setLevel(previous)


def test_content_maps_catalog_fields_to_nested_normalized_record():
    from scripts.upsert_logan_content import CONTENT

    source = json.loads((CONTENT / "aws_cloudtrail_ocsf.source.json").read_text())
    fields = json.loads((CONTENT / "aws_cloudtrail_ocsf.fields.json").read_text())["fields"]
    parser = json.loads((CONTENT / "aws_cloudtrail_ocsf.parser.json").read_text())["parser"]
    catalog = load_catalog().sources["aws.cloudtrail"]
    assert source["source"]["display_name"] == catalog["display_name"]
    assert source["catalog_mappings"] == catalog["fields"]
    # Built-in fields only: no custom-field quota is consumed in any tenancy.
    assert fields == []
    paths = {mapping["storage_field_name"]: mapping["structured_column_info"][2:] for mapping in parser["field_maps"]}
    assert paths == {display: key for key, display in catalog["fields"].items() if key != "log.source"}
    row = normalize_record(records()[0])
    for path in paths.values():
        value = row
        for key in path.split("."):
            value = value[key]
    assert parser["type"] == "JSON" and parser["is_single_line_content"] is True


def builtin_field_references():
    """Parser storage fields that no bundle defines, i.e. Log Analytics built-ins."""
    from scripts.upsert_logan_content import CONTENT

    refs = set()
    for parser_path in CONTENT.glob("*.parser.json"):
        fields = json.loads(parser_path.with_name(parser_path.name.replace(".parser.", ".fields.")).read_text())["fields"]
        defined = {f["name"] for f in fields} | {f["display_name"] for f in fields}
        refs |= {m["storage_field_name"] for m in json.loads(parser_path.read_text())["parser"]["field_maps"]} - defined
    return refs - {"Time"}


def test_every_bundle_uses_the_oci_json_parser_shape():
    """OCI JSON parsers carry the path in structured_column_info and a locale language.

    parser_field_expression is for regex parsers; the service rejects it on a JSON
    parser with "Invalid property name", which offline fakes cannot detect.
    """
    from scripts.upsert_logan_content import CONTENT

    for path in sorted(CONTENT.glob("*.parser.json")):
        parser = json.loads(path.read_text())["parser"]
        assert parser["type"] == "JSON" and parser["language"] == "en_US" and parser["header_content"] == "$", path.name
        for mapping in parser["field_maps"]:
            assert "parser_field_expression" not in mapping, path.name
            assert mapping["structured_column_info"].startswith("$."), path.name


def test_missing_or_custom_builtin_field_fails_closed_before_any_write():
    from scripts.upsert_logan_content import apply_content, load_content

    client = ContentClient()
    name = next(k for k, v in client.resources["field"].items() if v.display_name == "User Name")
    client.resources["field"][name] = client.models.LogAnalyticsField(name=name, display_name="User Name", data_type="STRING", is_system=False)
    with pytest.raises(RuntimeError, match="built-in Log Analytics field is unavailable: User Name"):
        apply_content(client, "ns", "compartment", load_content())
    assert not [call for call in client.calls if call[0] == "upsert"]
    del client.resources["field"][name]
    with pytest.raises(RuntimeError, match="User Name"):
        apply_content(client, "ns", "compartment", load_content())


class ContentClient:
    """Use actual OCI model instances to catch SDK-shape integration failures."""

    def __init__(self, conflict=False):
        import oci

        self.models = oci.log_analytics.models
        self.to_dict = oci.util.to_dict
        time = self.models.LogAnalyticsField(name="builtin-time", display_name="Time", data_type="TIMESTAMP", is_system=True)
        self.resources = {"field": {"builtin-time": time}, "parser": {}, "source": {}}
        for index, display in enumerate(sorted(builtin_field_references())):
            self.resources["field"][f"builtin-{index}"] = self.models.LogAnalyticsField(
                name=f"builtin-{index}", display_name=display, data_type="STRING", is_system=True)
        self.calls = []
        self.etag = "current-version"
        self.conflict = conflict

    def list_fields(self, namespace, **kwargs):
        return SimpleNamespace(data=SimpleNamespace(items=list(self.resources["field"].values())), headers={})

    def list_parsers(self, namespace, **kwargs):
        return SimpleNamespace(data=SimpleNamespace(items=list(self.resources["parser"].values())), headers={})

    def list_sources(self, namespace, compartment, **kwargs):
        return SimpleNamespace(data=SimpleNamespace(items=list(self.resources["source"].values())), headers={})

    def get_field(self, namespace, name):
        return self.get("field", name)

    def get_parser(self, namespace, name):
        return self.get("parser", name)

    def get_source(self, namespace, name, compartment):
        return self.get("source", name)

    def get(self, kind, name):
        self.calls.append(("get", kind, name))
        return SimpleNamespace(data=self.resources[kind][name], headers={"etag": self.etag} if self.etag else {})

    def upsert_field(self, namespace, body, **kwargs):
        return self.upsert("field", body, kwargs)

    def upsert_parser(self, namespace, body, **kwargs):
        return self.upsert("parser", body, kwargs)

    def upsert_source(self, namespace, body, **kwargs):
        return self.upsert("source", body, kwargs)

    def upsert(self, kind, body, kwargs):
        if self.conflict and kwargs.get("if_match"):
            raise RuntimeError("concurrent update")
        data = {key: value for key, value in self.to_dict(body).items() if value is not None}
        name = data.get("name") or f"generated-{kind}-{len(self.resources[kind])}"
        self.calls.append(("upsert", kind, data, kwargs))
        model_name = {"field": "LogAnalyticsField", "parser": "LogAnalyticsParser", "source": "LogAnalyticsSource"}[kind]
        from scripts.upsert_logan_content import _model

        result = _model(self.models, model_name, {**data, "name": name})
        self.resources[kind][name] = result
        return SimpleNamespace(data=result, headers={"etag": "new-version"})


def test_content_plan_is_read_only_and_apply_resolves_generated_names():
    from scripts.upsert_logan_content import apply_content, load_content, plan_content

    client = ContentClient()
    assets = load_content()
    result = plan_content(client, "sample", "sample-compartment", assets)
    assert result["counts"]["create"] == len(assets) == 2
    assert not any(call[0] == "upsert" for call in client.calls)
    applied = apply_content(client, "sample", "sample-compartment", assets)
    assert applied["counts"]["created"] == len(assets)
    upserts = [call for call in client.calls if call[0] == "upsert"]
    assert [call[1] for call in upserts] == ["parser", "source"]  # built-in fields are never upserted
    assert [call[2]["name"] for call in upserts] == ["AWS_CLOUDTRAIL_OCSF_PARSER", "AWS_CLOUDTRAIL_OCSF"]  # service requires them
    parser_maps = upserts[-2][2]["field_maps"]
    assert parser_maps[0]["storage_field_name"] == "builtin-time"
    assert all(mapping["storage_field_name"] in client.resources["field"] for mapping in parser_maps)
    assert upserts[-1][2]["parsers"][0]["name"] in client.resources["parser"]
    # Repeat with the same loaded assets detects mutation or incorrect SDK comparison.
    assert apply_content(client, "sample", "sample-compartment", assets)["counts"]["unchanged"] == len(assets)


def test_content_changes_use_etag_and_never_touch_builtin():
    from scripts.upsert_logan_content import apply_content, load_content

    client = ContentClient()
    apply_content(client, "sample", "sample-compartment")
    assets = load_content()
    field = assets[0]
    updated = replace(field, body={**field.body, "description": "updated description"})
    result = apply_content(client, "sample", "sample-compartment", (updated, *assets[1:]))
    assert result["counts"]["updated"] == 1
    assert client.calls[-1][0] == "get"  # source is unchanged
    updated_call = [call for call in client.calls if call[0] == "upsert"][-1]
    assert updated_call[3]["if_match"] == "current-version"
    assert len(updated_call[3]["opc_retry_token"]) == 64
    assert updated_call[2]["name"] != "builtin-time"


@pytest.mark.parametrize("missing_token", [False, True])
def test_content_update_fails_closed_on_conflict_or_missing_token(missing_token):
    from scripts.upsert_logan_content import apply_content, load_content

    client = ContentClient()
    apply_content(client, "sample", "sample-compartment")
    assets = load_content()
    assets = (replace(assets[0], body={**assets[0].body, "description": "changed"}), *assets[1:])
    before = sum(call[0] == "upsert" for call in client.calls)
    if missing_token:
        client.etag = None
    else:
        client.conflict = True
    with pytest.raises(RuntimeError):
        apply_content(client, "sample", "sample-compartment", assets)
    assert sum(call[0] == "upsert" for call in client.calls) == before


def test_content_parser_only_change_is_applied_with_concurrency_token():
    from scripts.upsert_logan_content import apply_content, load_content

    client = ContentClient()
    apply_content(client, "sample", "sample-compartment")
    assets = list(load_content())
    item = assets[-2]
    mappings = [{**mapping} for mapping in item.body["field_maps"]]
    mappings[-1]["structured_column_info"] = "$.changed_error_code"
    assets[-2] = replace(item, body={**item.body, "field_maps": mappings})
    assert apply_content(client, "sample", "sample-compartment", tuple(assets))["counts"]["updated"] == 1
    updated = [call for call in client.calls if call[0] == "upsert"][-1]
    assert updated[1] == "parser" and updated[3]["if_match"] == "current-version"


@pytest.mark.skipif(importlib.util.find_spec("botocore") is None,
                    reason="botocore is a collector dependency (collectors/logs/requirements.txt)")
def test_web_identity_sts_assumption_is_lazy_and_offline(monkeypatch, tmp_path):
    import botocore.credentials
    import botocore.session
    from botocore import UNSIGNED
    from botocore.config import Config
    from botocore.stub import Stubber
    from datetime import datetime, timedelta, timezone
    from collectors.logs.runtime import build_aws_session

    token = tmp_path / "token"
    token.write_text("synthetic-oidc-token")
    base = botocore.session.Session()
    sts = base.create_client("sts", region_name="us-east-1", config=Config(signature_version=UNSIGNED))
    stub = Stubber(sts)
    stub.add_response("assume_role_with_web_identity", {
        "Credentials": {"AccessKeyId": "ASIA" + "S" * 16, "SecretAccessKey": "s" * 40, "SessionToken": "synthetic-session-token",
                        "Expiration": datetime.now(timezone.utc) + timedelta(hours=1)},
        "AssumedRoleUser": {"AssumedRoleId": "sample:session", "Arn": "arn:aws:sts::123456789012:assumed-role/sample-collector/session"},
    }, {"RoleArn": config().aws_role_arn, "RoleSessionName": config().role_session_name, "WebIdentityToken": "synthetic-oidc-token"})
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    stub.add_response("assume_role_with_web_identity", {
        "Credentials": {"AccessKeyId": "ASIA" + "R" * 16, "SecretAccessKey": "r" * 40, "SessionToken": "rotated-session-token",
                        "Expiration": future + timedelta(hours=1)},
        "AssumedRoleUser": {"AssumedRoleId": "sample:session", "Arn": "arn:aws:sts::123456789012:assumed-role/sample-collector/session"},
    }, {"RoleArn": config().aws_role_arn, "RoleSessionName": config().role_session_name, "WebIdentityToken": "synthetic-rotated-oidc-token"})
    base.create_client = lambda *args, **kwargs: sts
    calls = iter([base, botocore.session.Session()])
    monkeypatch.setattr(botocore.session, "get_session", lambda: next(calls))
    with stub:
        aws = build_aws_session(replace(config(), web_identity_token_file=str(token)))
        credentials = aws.get_credentials()
        assert credentials.method == "assume-role-with-web-identity"
        assert credentials.get_frozen_credentials().token == "synthetic-session-token"
        token.write_text("synthetic-rotated-oidc-token")
        credentials._time_fetcher = lambda: future
        monkeypatch.setattr(botocore.credentials, "_local_now", lambda: future)
        assert credentials.get_frozen_credentials().token == "rotated-session-token"
        stub.assert_no_pending_responses()


def test_uploader_uses_actual_content_source_identifier():
    from scripts.upsert_logan_content import apply_content

    client = ContentClient()
    apply_content(client, "sample-namespace", "sample-compartment")
    source_id = next(iter(client.resources["source"]))
    assert source_id != config().source_name
    client.get_log_analytics_log_group = lambda namespace, group: SimpleNamespace(data=SimpleNamespace(compartment_id="sample-compartment"))
    accepted = []

    def upload(**kwargs):
        assert kwargs["log_source_name"] == source_id
        accepted.append(kwargs["upload_log_file_body"].read())
        return SimpleNamespace(status=200)

    client.upload_log_file = upload
    events = []
    collector = Collector(config(), sqs=Queue(events), s3=Objects(), logan=client)
    assert collector.process_message(message()) == "uploaded"
    assert len(accepted) == 1 and events[-1] == "delete"


def test_ambiguous_catalog_source_is_never_uploaded_or_acked():
    collector, events, _, _, logan = pipeline()
    source = SimpleNamespace(display_name=config().source_name, name="sample-source")
    logan.list_sources = lambda *args, **kwargs: SimpleNamespace(data=SimpleNamespace(items=[source, source]), headers={})
    assert collector.process_message(message()) == "retry"
    assert not logan.uploads and "delete" not in events


def test_canonical_cloudtrail_path_and_record_limits():
    collector, events, _, s3, _ = pipeline()
    forged_key = KEY.replace("/CloudTrail/", "/untrusted/CloudTrail/")
    assert collector.process_message(message(key=forged_key)) == "retry"
    assert not s3.calls and "delete" not in events
    limited, events, _, _, logan = pipeline(config(max_records_per_object=2))
    assert limited.process_message(message()) == "retry"
    assert not logan.uploads and "delete" not in events


def test_url_encoded_s3_object_key_is_decoded():
    from urllib.parse import quote_plus

    collector, _, _, s3, _ = pipeline()
    assert collector.process_message(message(key=quote_plus(KEY))) == "uploaded"
    assert s3.calls[0]["Key"] == KEY
    assert s3.calls[0]["ExpectedBucketOwner"] == config().account_id


def test_content_plan_and_apply_agree_with_service_enrichment():
    from scripts.upsert_logan_content import apply_content, load_content, plan_content

    client = ContentClient()
    apply_content(client, "sample", "sample-compartment")
    parser = next(iter(client.resources["parser"].values()))
    parser.field_maps[1].parser_field_id = 456
    parser.field_maps[1].field = client.models.LogAnalyticsField(name="provider-added", description="provider metadata")
    source = next(iter(client.resources["source"].values()))
    source.parsers[0].description = "provider-added parser description"
    source.parsers[0].edit_version = 2
    assert plan_content(client, "sample", "sample-compartment")["counts"] == {"create": 0, "update": 0, "unchanged": len(load_content())}
    before = len([call for call in client.calls if call[0] == "upsert"])
    assert apply_content(client, "sample", "sample-compartment")["counts"]["unchanged"] == len(load_content())
    assert before == len([call for call in client.calls if call[0] == "upsert"])


def test_plan_detects_one_changed_owned_property():
    from scripts.upsert_logan_content import apply_content, load_content, plan_content

    client = ContentClient()
    apply_content(client, "sample", "sample-compartment")
    assets = load_content()
    changed = replace(assets[0], body={**assets[0].body, "description": "changed"})
    planned = plan_content(client, "sample", "sample-compartment", (changed, *assets[1:]))
    assert planned["counts"] == {"create": 0, "update": 1, "unchanged": len(load_content()) - 1}


def test_default_cli_content_mode_is_plan(monkeypatch, capsys):
    from scripts import upsert_logan_content as content

    client = ContentClient()
    monkeypatch.setattr(content, "build_client", lambda: client)
    assert content.main(["--namespace", "sample-private-namespace", "--compartment-id", "sample-private-compartment"]) == 0
    assert not any(call[0] == "upsert" for call in client.calls)
    assert "sample-private" not in capsys.readouterr().out


def test_duplicate_content_display_name_fails_without_writes():
    from scripts.upsert_logan_content import load_content, plan_content

    client = ContentClient()
    client.resources["field"]["duplicate-1"] = client.models.LogAnalyticsField(name="duplicate-1", display_name="User Name", is_system=True)
    with pytest.raises(ValueError):
        plan_content(client, "sample", "sample-compartment")
    assert not any(call[0] == "upsert" for call in client.calls)


def test_collector_cli_help_works_outside_backend_import_path():
    import subprocess
    import sys

    result = subprocess.run([sys.executable, "-m", "collectors.logs", "--help"],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and "--once" in result.stdout


def test_source_discovery_is_paginated_and_cached_per_collector():
    collector, _, _, _, logan = pipeline()
    calls = []

    def sources(namespace, compartment, **kwargs):
        calls.append(kwargs)
        if not kwargs:
            return SimpleNamespace(data=SimpleNamespace(items=[]), headers={"opc-next-page": "second"})
        return SimpleNamespace(data=SimpleNamespace(items=[SimpleNamespace(display_name=config().source_name, name="second-page-source")]), headers={})

    logan.list_sources = sources
    assert collector.process_message(message()) == "uploaded"
    assert collector.process_message(message(count=2)) == "uploaded"
    assert calls == [{}, {"page": "second"}]
    assert all(upload["log_source_name"] == "second-page-source" for upload in logan.uploads)


@pytest.mark.parametrize("failure", ["missing_source", "missing_compartment", "repeated_page"])
def test_source_discovery_failure_leaves_message(failure):
    collector, events, _, _, logan = pipeline()
    if failure == "missing_compartment":
        logan.get_log_analytics_log_group = lambda *args: SimpleNamespace(data=SimpleNamespace(compartment_id=None))
    else:
        logan.list_sources = lambda *args, **kwargs: SimpleNamespace(
            data=SimpleNamespace(items=[]), headers={"opc-next-page": "repeated"} if failure == "repeated_page" else {})
    assert collector.process_message(message()) == "retry"
    assert not logan.uploads and "delete" not in events


@pytest.mark.skipif(importlib.util.find_spec("botocore") is None,
                    reason="botocore is a collector dependency (collectors/logs/requirements.txt)")
def test_external_id_uses_refreshable_temporary_principal(monkeypatch):
    import botocore.session
    from botocore.credentials import Credentials
    from botocore.stub import Stubber
    from datetime import datetime, timedelta, timezone
    from collectors.logs.runtime import build_aws_session

    base = botocore.session.Session()
    temporary = Credentials("ASIA" + "P" * 16, "p" * 40, "temporary-parent-token", method="container-role")
    base.get_credentials = lambda: temporary
    sts = base.create_client("sts", region_name="us-east-1", aws_access_key_id=temporary.access_key,
                             aws_secret_access_key=temporary.secret_key, aws_session_token=temporary.token)
    stub = Stubber(sts)
    stub.add_response("assume_role", {
        "Credentials": {"AccessKeyId": "ASIA" + "S" * 16, "SecretAccessKey": "s" * 40, "SessionToken": "assumed-session-token",
                        "Expiration": datetime.now(timezone.utc) + timedelta(hours=1)},
        "AssumedRoleUser": {"AssumedRoleId": "sample:session", "Arn": "arn:aws:sts::123456789012:assumed-role/sample-collector/session"},
    }, {"RoleArn": config().aws_role_arn, "RoleSessionName": config().role_session_name, "ExternalId": "synthetic-external-id"})
    base.create_client = lambda *args, **kwargs: sts
    sessions = iter([base, botocore.session.Session()])
    monkeypatch.setattr(botocore.session, "get_session", lambda: next(sessions))
    with stub:
        cfg = replace(config(), aws_auth_mode="external_id", web_identity_token_file="", external_id="synthetic-external-id")
        credentials = build_aws_session(cfg).get_credentials()
        assert credentials.method == "assume-role"
        assert credentials.get_frozen_credentials().token == "assumed-session-token"
        stub.assert_no_pending_responses()


@pytest.mark.skipif(importlib.util.find_spec("botocore") is None,
                    reason="botocore is a collector dependency (collectors/logs/requirements.txt)")
def test_external_id_rejects_static_parent_credentials(monkeypatch):
    import botocore.session
    from botocore.credentials import Credentials
    from collectors.logs.runtime import build_aws_session

    base = botocore.session.Session()
    base.get_credentials = lambda: Credentials("synthetic-static-id", "synthetic-static-secret", method="shared-credentials-file")
    monkeypatch.setattr(botocore.session, "get_session", lambda: base)
    cfg = replace(config(), aws_auth_mode="external_id", web_identity_token_file="", external_id="synthetic-external-id")
    with pytest.raises(ValueError):
        build_aws_session(cfg)


def test_terraform_regexes_respect_re2_repeat_limit():
    # Live plan found {2,1224}: RE2 (Terraform) rejects repeat counts above 1000,
    # so can(regex(...)) silently returned false and refused every external ID.
    import re

    root = Path(__file__).resolve().parents[1] / "modules" / "logs"
    offenders = []
    for path in root.rglob("*.tf"):
        for match in re.finditer(r"\{(\d+)(?:,(\d+))?\}", path.read_text(encoding="utf-8")):
            if max(int(n) for n in match.groups() if n) > 1000:
                offenders.append(f"{path.relative_to(root)}: {match.group(0)}")
    assert not offenders, offenders


# --- explicit terminal reason codes -----------------------------------------

def _multi(*keys):
    body = {"Records": [{"eventSource": "aws:s3", "eventName": "ObjectCreated:Put", "awsRegion": "us-east-1",
                         "s3": {"bucket": {"name": "sample-cloudtrail"}, "object": {"key": key}}} for key in keys]}
    return {"Body": json.dumps(body), "ReceiptHandle": "sample-receipt", "MessageId": "m",
            "Attributes": {"ApproximateReceiveCount": "1"}}


def _result_logs(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("ingest_message_result")]


def test_reason_s3_test_event(caplog):
    collector, events, _, _, _ = pipeline()
    test_message = message()
    test_message["Body"] = json.dumps({"Event": "s3:TestEvent", "Bucket": "sample-cloudtrail"})
    with caplog.at_level(logging.INFO):
        outcome, reason, stats = collector.process_message_detail(test_message)
    assert (outcome, reason) == ("ignored", "s3_test_event")
    assert stats["references"] == 0 and events[-1] == "delete"
    assert "reason=s3_test_event" in _result_logs(caplog)[0]


def test_test_event_requires_exact_envelope():
    collector, events, _, _, _ = pipeline()
    for body in ({"Event": "s3:TestEvent", "Bucket": "other-bucket"}, {"Event": "s3:Other", "Bucket": "sample-cloudtrail"}):
        wrong = message(count=1)
        wrong["Body"] = json.dumps(body)
        outcome, reason, _ = collector.process_message_detail(wrong)
        assert outcome == "retry" and reason == "retry_invalid:notification"
    assert "delete" not in events


def test_reason_digest_only():
    collector, events, _, s3, _ = pipeline()
    digest = "cloudtrail/AWSLogs/123456789012/CloudTrail-Digest/us-east-1/2026/01/02/123456789012_CloudTrail-Digest_us-east-1_t_us-east-1_20260102T000000Z.json.gz"
    outcome, reason, stats = collector.process_message_detail(message(key=digest))
    assert (outcome, reason) == ("ignored", "digest_only")
    assert stats["digest_skipped"] == 1 and stats["references"] == 0 and not s3.calls and events[-1] == "delete"


def test_substring_digest_in_data_key_is_data_not_digest():
    collector, events, _, s3, _ = pipeline()
    sneaky = KEY.replace("/CloudTrail/", "/CloudTrail/us-east-1/CloudTrail-Digest/", 1)
    outcome, reason, stats = collector.process_message_detail(message(key=sneaky))
    assert reason == "retry_invalid:scope" or reason == "retry_invalid:notification"
    assert stats["digest_skipped"] == 0 and not s3.calls and "delete" not in events


def test_mixed_digest_and_data_uploads_before_delete():
    collector, events, _, s3, _ = pipeline()
    digest = "cloudtrail/AWSLogs/123456789012/CloudTrail-Digest/us-east-1/2026/01/02/x.json.gz"
    outcome, reason, stats = collector.process_message_detail(_multi(digest, KEY))
    assert (outcome, reason) == ("uploaded", "uploaded")
    assert stats["digest_skipped"] == 1 and stats["references"] == 1 and stats["batches_uploaded"] >= 1
    assert stats["records"] > 0 and len(s3.calls) == 1
    assert events.index("upload") < events.index("delete")


def test_process_message_still_returns_plain_outcome():
    collector, _, _, _, _ = pipeline()
    assert collector.process_message(message()) == "uploaded"


@pytest.mark.parametrize("data,stage", [(b"not gzip", "object_decode")])
def test_dlq_reason_has_stage(data, stage):
    collector, events, _, _, _ = pipeline(data=data)
    outcome, reason, _ = collector.process_message_detail(message(count=5))
    assert (outcome, reason) == ("dead_lettered", f"dlq_invalid:{stage}")
    assert events[-2:] == ["dlq", "delete"]


def test_dlq_stage_notification_normalize_and_limits():
    collector, _, _, _, _ = pipeline()
    bad = message(count=5)
    bad["Body"] = "garbage"
    assert collector.process_message_detail(bad)[1] == "dlq_invalid:notification"
    row = records()
    row[-1]["eventTime"] = "invalid"
    c2, _, _, _, _ = pipeline(data=gzip.compress(json.dumps({"Records": row}).encode()))
    assert c2.process_message_detail(message(count=5))[1] == "dlq_invalid:normalize"
    c3, _, _, _, _ = pipeline(config(max_records_per_message=1))
    assert c3.process_message_detail(message(count=5))[1] == "dlq_invalid:limits"


def test_provider_failure_reason():
    collector, events, _, _, _ = pipeline(fail_at=1)
    outcome, reason, _ = collector.process_message_detail(message())
    assert (outcome, reason) == ("retry", "retry_provider_failure") and "delete" not in events


def test_poll_counts_include_reason_aggregates():
    test_message = message()
    test_message["Body"] = json.dumps({"Event": "s3:TestEvent", "Bucket": "sample-cloudtrail"})
    collector, _, _, _, _ = pipeline(messages=[test_message])
    counts = collector.poll_once()
    assert counts["ignored"] == 1 and counts["reason.s3_test_event"] == 1


def test_poll_collector_logs_summary_in_once_and_continuous(caplog, monkeypatch):
    from collectors.logs import __main__ as entry
    calls = {"n": 0}

    class Fake:
        def poll_once(self):
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt
            return {"uploaded": 0, "ignored": 1, "retry": 0, "dead_lettered": 0, "reason.digest_only": 1}

    with caplog.at_level(logging.INFO):
        assert entry.poll_collector(Fake(), once=True) == 0
        calls["n"] = 0
        assert entry.poll_collector(Fake(), once=False) == 0
    summaries = [r.getMessage() for r in caplog.records if r.getMessage().startswith("ingest_poll_summary")]
    assert len(summaries) == 2 and "reason.digest_only=1" in summaries[0]
