"""Azure diagnostic records; optional SDKs are imported only at the runtime edge."""

from __future__ import annotations

import ipaddress
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Event as StopEvent, Thread
from typing import Any, Mapping
from urllib.parse import urlparse

from ..catalog import load_catalog
from ..normalize import InvalidMessage, _string, batch_ndjson
from ..upload import LoganUploader
from ..poison import FilePoisonStore

logger = logging.getLogger("mc.Ingest.Azure")

GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
ACTIVITY_CATEGORIES = {"Administrative", "Security", "ServiceHealth", "Alert", "Recommendation",
                       "Policy", "Autoscale", "ResourceHealth", "Write", "Read", "Delete", "Action"}


def _object(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise InvalidMessage("invalid Azure object field")
    return value


def _value(value: Any) -> str | None:
    return _string(value.get("value")) if isinstance(value, Mapping) else _string(value)


def normalize_azure_record(record: Mapping[str, Any], *, tenant_id: str | None = None) -> tuple[str, dict[str, Any]]:
    """Allowlist catalog fields; subscription scope for Activity, tenant for Entra.

    tenant_id binds Entra exports that omit tenantId to the collector's approved
    tenant. An explicit conflicting tenant remains visible for scope rejection.
    """
    if not isinstance(record, Mapping):
        raise InvalidMessage("Azure record is not an object")
    props = _object(record.get("properties"))
    category = _string(record.get("category"))
    resource = _string(record.get("resourceId"))
    if category in ACTIVITY_CATEGORIES:
        key = "azure.activity"
        match = re.match(r"^/subscriptions/([^/]+)(?:/|$)", resource or "", re.I)
        account = _string(record.get("subscriptionId")) or (match[1] if match else None)
        if match and account and match[1].lower() != account.lower():
            raise InvalidMessage("conflicting Azure subscription")
        identity = _object(record.get("identity"))
        claims = _object(identity.get("claims"))
        user = (_string(record.get("caller")) or _string(claims.get("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn"))
                or _string(claims.get("name")) or _string(claims.get("appid")))
        address = (_string(record.get("callerIpAddress")) or _string(claims.get("ipaddr"))
                   or _string(_object(identity.get("authorization")).get("clientIpAddress")))
        outcome = _value(record.get("resultType")) or _value(record.get("status"))
    elif category in {"SignInLogs", "AuditLogs"}:
        key = "azure.entra_signin" if category == "SignInLogs" else "azure.entra_audit"
        match = re.match(r"^/tenants/([^/]+)(?:/|$)", resource or "", re.I)
        explicit = [_string(record.get("tenantId")), _string(props.get("tenantId")), match[1] if match else None]
        if len({value.lower() for value in explicit if value}) > 1:
            raise InvalidMessage("conflicting Entra tenant")
        account = next((value for value in explicit if value), tenant_id)
        if category == "SignInLogs":
            user = _string(props.get("userPrincipalName")) or _string(props.get("userId"))
            address = _string(props.get("ipAddress"))
            code = _object(props.get("status")).get("errorCode", record.get("resultType"))
            if type(code) is int or (isinstance(code, str) and code.isdecimal()):
                outcome = "Success" if int(code) == 0 else "Failure"
            else:
                outcome = None
            resource = _string(props.get("resourceDisplayName")) or _string(props.get("resourceId")) or resource
        else:
            initiated = _object(props.get("initiatedBy"))
            principal, app = _object(initiated.get("user")), _object(initiated.get("app"))
            user = _string(principal.get("userPrincipalName")) or _string(app.get("displayName")) or _string(app.get("appId"))
            address = _string(principal.get("ipAddress"))
            outcome = _string(props.get("result")) or _value(record.get("resultType"))
            targets = props.get("targetResources", [])
            if not isinstance(targets, list) or any(not isinstance(item, Mapping) for item in targets):
                raise InvalidMessage("invalid Azure audit targets")
            resource = ", ".join(_string(item.get("id")) or _string(item.get("displayName")) or "" for item in targets) or resource
    else:
        raise InvalidMessage("unsupported Azure diagnostic category")
    if not isinstance(account, str) or not GUID.fullmatch(account):
        raise InvalidMessage("Azure scope GUID missing")
    timestamp = _string(record.get("time")) or _string(record.get("eventTimestamp"))
    activity = _value(record.get("operationName")) or _string(props.get("activityDisplayName"))
    if not timestamp or not activity:
        raise InvalidMessage("required Azure event field missing")
    try:
        instant = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError
        timestamp = instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        raise InvalidMessage("invalid Azure timestamp") from None
    try:
        ip = str(ipaddress.ip_address(address)) if address else None
    except ValueError:
        ip = None
    status = {"success": "Success", "succeeded": "Success", "failure": "Failure", "failed": "Failure"}.get((outcome or "").lower(), "Unknown")
    return key, {
        "timestamp": timestamp, "cloud": {"provider": "azure", "account": {"uid": account.lower()},
                                           "region": _string(record.get("resourceLocation")) or _string(props.get("resourceLocation"))},
        "actor": {"user": {"name": user}}, "src_endpoint": {"ip": ip}, "activity_name": activity,
        "status": status, "resource": resource, "trace_id": _string(record.get("correlationId")) or _string(props.get("correlationId")),
        "event_uid": _string(record.get("eventDataId")) or _string(props.get("id")), "service_name": category,
        "source_address": address,
    }


@dataclass(frozen=True, repr=False)
class AzureCollectorConfig:
    namespace: str
    log_group_ocid: str
    subscription_id: str
    tenant_id: str
    fully_qualified_namespace: str
    eventhub_name: str
    consumer_group: str
    dlq_directory: str
    source_names: Mapping[str, str] = field(default_factory=lambda: {
        key: load_catalog().source_name(key) for key in ("azure.activity", "azure.entra_signin", "azure.entra_audit")
    })
    max_batch_records: int = 500
    max_batch_bytes: int = 4 * 1024 * 1024
    max_message_bytes: int = 8 * 1024 * 1024
    max_records_per_message: int = 10000
    max_receive_count: int = 5
    max_received_batch: int = 100
    oci_auth_mode: str = "resource_principal"
    oci_region: str = ""

    def __post_init__(self) -> None:
        for name in ("namespace", "eventhub_name", "consumer_group", "dlq_directory"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"missing Azure collector setting: {name}")
        for name in ("subscription_id", "tenant_id"):
            if not isinstance(getattr(self, name), str) or not GUID.fullmatch(getattr(self, name)):
                raise ValueError(f"invalid Azure collector GUID: {name}")
        if not Path(self.dlq_directory).is_absolute():
            raise ValueError("absolute durable DLQ directory required")
        if not self.log_group_ocid.startswith("ocid1.loganalyticsloggroup."):
            raise ValueError("Log Analytics log group required")
        if not re.fullmatch(r"[a-zA-Z0-9-]+\.servicebus\.(windows\.net|usgovcloudapi\.net|chinacloudapi\.cn)", self.fully_qualified_namespace):
            raise ValueError("exact Azure Event Hubs namespace required")
        if self.oci_auth_mode not in {"resource_principal", "instance_principal"}:
            raise ValueError("OCI workload identity required")
        if set(self.source_names) != {"azure.activity", "azure.entra_signin", "azure.entra_audit"} or any(not isinstance(v, str) or not v.strip() for v in self.source_names.values()):
            raise ValueError("all Azure catalog sources required")
        for name, bound in {"max_batch_records": 10000, "max_batch_bytes": 64 * 1024 * 1024,
                            "max_message_bytes": 32 * 1024 * 1024, "max_records_per_message": 100000,
                            "max_receive_count": 1000, "max_received_batch": 1000}.items():
            if type(getattr(self, name)) is not int or not 1 <= getattr(self, name) <= bound:
                raise ValueError(f"invalid Azure collector limit: {name}")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AzureCollectorConfig:
        env = os.environ if env is None else env
        if any(env.get(key) for key in ("AZURE_CLIENT_SECRET", "AZURE_EVENTHUB_CONNECTION_STRING", "AZURE_STORAGE_CONNECTION_STRING", "AZURE_STORAGE_KEY")):
            raise ValueError("Azure shared credentials prohibited")
        catalog = load_catalog(env.get("MC_LOGAN_SOURCES_PATH") or None)
        names = {key: catalog.source_name(key, tenant_id=env.get("MC_TENANT_ID"))
                 for key in ("azure.activity", "azure.entra_signin", "azure.entra_audit")}
        numbers = {name: int(env[key]) for name, key in {
            "max_batch_records": "INGEST_MAX_BATCH_RECORDS", "max_batch_bytes": "INGEST_MAX_BATCH_BYTES",
            "max_message_bytes": "INGEST_MAX_MESSAGE_BYTES", "max_records_per_message": "INGEST_MAX_RECORDS_PER_MESSAGE",
            "max_receive_count": "INGEST_MAX_RECEIVE_COUNT", "max_received_batch": "AZURE_MAX_RECEIVED_BATCH",
        }.items() if env.get(key)}
        return cls(namespace=env.get("LOGAN_NAMESPACE", ""), log_group_ocid=env.get("LOGAN_LOG_GROUP_OCID", ""),
                   subscription_id=env.get("AZURE_SUBSCRIPTION_ID", ""), tenant_id=env.get("AZURE_TENANT_ID", ""),
                   fully_qualified_namespace=env.get("AZURE_EVENTHUB_NAMESPACE", ""), eventhub_name=env.get("AZURE_EVENTHUB_NAME", ""),
                   consumer_group=env.get("AZURE_EVENTHUB_CONSUMER_GROUP", ""), dlq_directory=env.get("AZURE_DLQ_DIRECTORY", ""),
                   source_names=names, oci_auth_mode=env.get("OCI_AUTH_MODE", "resource_principal"), oci_region=env.get("OCI_REGION", ""), **numbers)


class AzureCollector:
    """Per-partition at-least-once adapter using the shared uploader and batcher."""

    def __init__(self, config: AzureCollectorConfig, *, logan: Any, poison_store: Any = None):
        self.config = config
        self.uploader = LoganUploader(config, logan)
        self.poison_store = poison_store if poison_store is not None else FilePoisonStore(config.dlq_directory)
        self.blocked_partitions: set[str] = set()

    def _process_event(self, partition: Any, event: Any) -> str:
        decode_failed = False
        try:
            raw = event.body_as_str(encoding="UTF-8")
            body_digest = hashlib.sha256(raw.encode()).hexdigest()
        except UnicodeDecodeError as exc:
            raw = ""
            body_digest = hashlib.sha256(exc.object).hexdigest()
            decode_failed = True
        identity = json.dumps([self.config.namespace, self.config.log_group_ocid,
                               self.config.fully_qualified_namespace, self.config.eventhub_name,
                               self.config.consumer_group, partition.partition_id, event.sequence_number, event.offset])
        digest = hashlib.sha256(identity.encode()).hexdigest()
        try:
            if decode_failed or not isinstance(raw, str) or len(raw.encode()) > self.config.max_message_bytes:
                raise InvalidMessage("Azure message exceeds byte bound")
            try:
                document = json.loads(raw)
            except (ValueError, RecursionError):
                raise InvalidMessage("invalid Azure diagnostic JSON") from None
            records = document.get("records") if isinstance(document, Mapping) else None
            if not isinstance(records, list) or not 1 <= len(records) <= self.config.max_records_per_message:
                raise InvalidMessage("Azure message has no bounded records")
            grouped: dict[str, list[dict[str, Any]]] = {}
            for record in records:
                key, row = normalize_azure_record(record, tenant_id=self.config.tenant_id)
                expected = self.config.subscription_id if key == "azure.activity" else self.config.tenant_id
                if row["cloud"]["account"]["uid"] != expected.lower():
                    raise InvalidMessage("Azure record outside collector scope")
                grouped.setdefault(key, []).append(row)
            try:
                uploads = [(key, payload) for key, rows in grouped.items() for payload in batch_ndjson(
                    rows, max_records=self.config.max_batch_records, max_bytes=self.config.max_batch_bytes)]
            except UnicodeError:
                raise InvalidMessage("invalid Azure field encoding") from None
            if sum(len(payload) for _, payload in uploads) > self.config.max_message_bytes:
                raise InvalidMessage("Azure normalized message exceeds byte bound")
        except InvalidMessage:
            logger.warning("ingest_invalid_message")
            envelope = {"schema_version": "mcingest.poison.v1", "reason": "invalid_message",
                        "body_sha256": body_digest,
                        "partition_id": partition.partition_id, "sequence_number": event.sequence_number,
                        "offset": event.offset}
            count = self.poison_store.record_failure(digest, envelope)
            if count < self.config.max_receive_count:
                return "retry"
            self.poison_store.send(digest, {**envelope, "receive_count": count})
            partition.update_checkpoint(event)
            return "dead_lettered"
        for key, payload in uploads:
            upload_identity = json.dumps([self.config.namespace, self.config.log_group_ocid, self.config.source_names[key], identity])
            self.uploader.upload(payload, upload_identity, source_name=self.config.source_names[key], filename_prefix=key.replace(".", "-"))
        partition.update_checkpoint(event)
        return "uploaded"

    def process_batch(self, partition: Any, events: list[Any]) -> str:
        """A failed partition stays blocked until restart from persisted checkpoint.

        Azure may invoke another callback after a callback error. This guard
        ensures that later events can never checkpoint over a failed event.
        """
        if partition.partition_id in self.blocked_partitions:
            return "retry"
        if not events:
            return "ignored"
        try:
            if len(events) > self.config.max_received_batch:
                raise RuntimeError("Azure received batch exceeds bound")
            result = "ignored"
            for event in events:
                result = self._process_event(partition, event)
                if result == "retry":
                    self.blocked_partitions.add(partition.partition_id)
                    return result
            return result
        except Exception:
            self.blocked_partitions.add(partition.partition_id)
            logger.warning("ingest_provider_failure_retry")
            return "retry"


def build_azure_runtime(config: AzureCollectorConfig, env: Mapping[str, str] | None = None) -> tuple[AzureCollector, Any, Any]:
    """No default-credential chain, client secret, SAS or account-key fallback."""
    env = os.environ if env is None else env
    client_id, token_file = env.get("AZURE_CLIENT_ID", ""), env.get("AZURE_FEDERATED_TOKEN_FILE", "")
    url, container = env.get("AZURE_CHECKPOINT_ACCOUNT_URL", ""), env.get("AZURE_CHECKPOINT_CONTAINER", "")
    parsed = urlparse(url)
    if (not GUID.fullmatch(client_id) or not token_file or not Path(token_file).is_absolute()
            or parsed.scheme != "https" or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment
            or parsed.path not in ("", "/") or not re.fullmatch(r"[a-z0-9]{3,24}\.blob\.(core\.windows\.net|core\.usgovcloudapi\.net|core\.chinacloudapi\.cn)", parsed.hostname or "")
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])", container) or "--" in container):
        raise ValueError("explicit workload identity and durable checkpoint container required")
    if any(env.get(key) for key in ("AZURE_CLIENT_SECRET", "AZURE_EVENTHUB_CONNECTION_STRING", "AZURE_STORAGE_CONNECTION_STRING", "AZURE_STORAGE_KEY")):
        raise ValueError("Azure shared credentials prohibited")
    from azure.identity import WorkloadIdentityCredential
    from azure.eventhub import EventHubConsumerClient
    from azure.eventhub.extensions.checkpointstoreblob import BlobCheckpointStore
    import oci
    from collectors.common.oci_auth import build_oci_client
    from ..runtime import suppress_sdk_payload_logging

    suppress_sdk_payload_logging()
    credential = WorkloadIdentityCredential(tenant_id=config.tenant_id, client_id=client_id, token_file_path=token_file,
                                            authority=env.get("AZURE_AUTHORITY_HOST", "https://login.microsoftonline.com"))
    consumer = checkpoint = None
    try:
        checkpoint = BlobCheckpointStore(blob_account_url=url, container_name=container, credential=credential)
        consumer = EventHubConsumerClient(fully_qualified_namespace=config.fully_qualified_namespace,
                                         eventhub_name=config.eventhub_name, consumer_group=config.consumer_group,
                                         credential=credential, checkpoint_store=checkpoint, logging_enable=False)
        adapter = AzureCollector(config, logan=build_oci_client(oci.log_analytics.LogAnalyticsClient,
                                 auth_mode=config.oci_auth_mode, region=config.oci_region or None, timeout=(10, 30)))
        return adapter, consumer, credential
    except Exception:
        if consumer is not None:
            consumer.close()
        if checkpoint is not None:
            checkpoint.close()
        credential.close()
        raise


def receive_azure(adapter: AzureCollector, consumer: Any) -> int:
    """Close from outside SDK callbacks on failure; restart replays the partition."""
    stopped, failed = StopEvent(), StopEvent()

    def on_batch(partition: Any, events: list[Any]) -> None:
        if stopped.is_set():
            return
        if adapter.process_batch(partition, events) == "retry":
            failed.set()
            stopped.set()

    def on_error(partition: Any, error: Exception) -> None:
        logger.warning("ingest_azure_receive_failed")
        failed.set()
        stopped.set()

    def receive() -> None:
        try:
            consumer.receive_batch(on_event_batch=on_batch, on_error=on_error, starting_position="-1",
                                   max_batch_size=adapter.config.max_received_batch,
                                   prefetch=adapter.config.max_received_batch, max_wait_time=5)
        except Exception:
            on_error(None, RuntimeError())
        finally:
            stopped.set()

    thread = Thread(target=receive, name="azure-ingest-receive", daemon=True)
    thread.start()
    try:
        while not stopped.wait(0.25):
            pass
    except KeyboardInterrupt:
        stopped.set()
    finally:
        consumer.close()
        thread.join(timeout=35)
    return 1 if failed.is_set() or thread.is_alive() else 0
