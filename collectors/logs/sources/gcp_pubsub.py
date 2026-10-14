"""Keyless GCP pull adapter using the shared catalog, batcher, DLQ and uploader."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from typing import Any, Iterator, Mapping
from urllib.parse import unquote

from ..catalog import load_catalog
from ..normalize import InvalidMessage, _string, batch_ndjson
from ..poison import FilePoisonStore
from ..upload import LoganUploader

logger = logging.getLogger("mc.Ingest.GCP")
PROJECT = r"[a-z][a-z0-9-]{4,28}[a-z0-9]"
RESOURCE_NAME = r"[A-Za-z][A-Za-z0-9._~+%-]{2,254}"
SOURCES = ("gcp.audit", "gcp.vpc_flow", "gcp.gke_audit")
AUDIT_LOGS = {"cloudaudit.googleapis.com/activity", "cloudaudit.googleapis.com/data_access"}


def _object(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise InvalidMessage("invalid GCP object field")
    return value


def _ip(address: str | None) -> str | None:
    try:
        return str(ipaddress.ip_address(address)) if address else None
    except ValueError:
        return None


def normalize_gcp_record(record: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Allowlist LogEntry fields, preserving project identity and UTC event time.

    Missing AuditLog status means OK by its protobuf contract. Malformed or
    unrecognized explicit status stays Unknown. Flow telemetry does not imply
    a successful API operation or an allow/deny firewall verdict.
    """
    if not isinstance(record, Mapping):
        raise InvalidMessage("GCP entry is not an object")
    log_name = _string(record.get("logName"))
    match = re.fullmatch(r"projects/(" + PROJECT + r")/logs/([^/]+)", log_name or "")
    if not match:
        raise InvalidMessage("canonical GCP project log name required")
    account, log_id = match[1], unquote(match[2])
    resource = _object(record.get("resource"))
    labels = _object(resource.get("labels"))
    project = _string(labels.get("project_id"))
    if project and project != account:
        raise InvalidMessage("conflicting GCP project identity")
    timestamp = _string(record.get("timestamp"))
    if not timestamp:
        raise InvalidMessage("GCP event timestamp required")
    try:
        instant = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError
        timestamp = instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        raise InvalidMessage("invalid GCP timestamp") from None
    dst, user, namespace = None, None, None
    if log_id in AUDIT_LOGS:
        payload = _object(record.get("protoPayload"))
        if payload.get("@type") != "type.googleapis.com/google.cloud.audit.AuditLog":
            raise InvalidMessage("GCP AuditLog payload required")
        service = _string(payload.get("serviceName"))
        activity = _string(payload.get("methodName"))
        if not service or not activity:
            raise InvalidMessage("GCP audit operation required")
        key = "gcp.gke_audit" if service == "k8s.io" else "gcp.audit"
        user = _string(_object(payload.get("authenticationInfo")).get("principalEmail"))
        address = _string(_object(payload.get("requestMetadata")).get("callerIp"))
        target = _string(payload.get("resourceName"))
        status_object = _object(payload.get("status"))
        code = status_object.get("code", 0)
        status = "Unknown"
        if (payload.get("status", {}) is not None and type(code) is int and 0 <= code <= 16):
            status = "Success" if code == 0 else "Failure"
        if key == "gcp.gke_audit":
            namespace_match = re.search(r"(?:^|/)namespaces/([^/]+)", target or "")
            namespace = namespace_match[1] if namespace_match else None
    elif log_id in {"compute.googleapis.com/vpc_flows", "networkmanagement.googleapis.com/vpc_flows"}:
        key = "gcp.vpc_flow"
        payload = _object(record.get("jsonPayload"))
        connection = _object(payload.get("connection"))
        address, dst = _string(connection.get("src_ip")), _ip(_string(connection.get("dest_ip")))
        if not address or not dst or not _ip(address):
            raise InvalidMessage("GCP flow connection required")
        service, activity, status = "compute.googleapis.com", "VPC Flow", "Unknown"
        target = _string(labels.get("subnetwork_id")) or _string(labels.get("subnetwork_name"))
    else:
        raise InvalidMessage("unsupported GCP log type")
    return key, {
        "timestamp": timestamp,
        "cloud": {"provider": "gcp", "account": {"uid": account},
                  "region": _string(labels.get("location")) or _string(labels.get("region")) or _string(labels.get("zone"))},
        "actor": {"user": {"name": user}}, "src_endpoint": {"ip": _ip(address)},
        "dst_endpoint": {"ip": dst}, "activity_name": activity, "status": status,
        "resource": target, "trace_id": _string(record.get("trace")), "event_uid": _string(record.get("insertId")),
        "service_name": service, "source_address": address, "k8s": {"namespace": {"name": namespace}},
    }


@dataclass(frozen=True, repr=False)
class GCPCollectorConfig:
    namespace: str
    log_group_ocid: str
    subscription: str
    dlq_topic: str
    allowed_project_ids: frozenset[str]
    dlq_directory: str
    source_names: Mapping[str, str] = field(default_factory=lambda: {key: load_catalog().source_name(key) for key in SOURCES})
    max_batch_records: int = 500
    max_batch_bytes: int = 4 * 1024 * 1024
    max_message_bytes: int = 10 * 1024 * 1024
    max_receive_count: int = 5
    ack_deadline_seconds: int = 300
    oci_auth_mode: str = "resource_principal"
    oci_region: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, str) or not self.namespace.strip():
            raise ValueError("Log Analytics namespace required")
        if not isinstance(self.log_group_ocid, str) or not self.log_group_ocid.startswith("ocid1.loganalyticsloggroup."):
            raise ValueError("Log Analytics log group required")
        if not re.fullmatch(r"projects/" + PROJECT + r"/subscriptions/" + RESOURCE_NAME, self.subscription):
            raise ValueError("exact GCP subscription required")
        topic_match = re.fullmatch(r"projects/(" + PROJECT + r")/topics/" + RESOURCE_NAME, self.dlq_topic)
        if not topic_match or topic_match[1] != self.subscription.split("/")[1]:
            raise ValueError("exact GCP DLQ topic in the transport project required")
        if (not isinstance(self.allowed_project_ids, frozenset) or not self.allowed_project_ids
                or any(not isinstance(value, str) or not re.fullmatch(PROJECT, value) for value in self.allowed_project_ids)):
            raise ValueError("exact allowed GCP project IDs required")
        if not self.dlq_directory or not Path(self.dlq_directory).is_absolute():
            raise ValueError("absolute durable DLQ directory required")
        if self.oci_auth_mode not in {"instance_principal", "resource_principal"}:
            raise ValueError("OCI workload identity required")
        if set(self.source_names) != set(SOURCES) or any(not isinstance(value, str) or not value.strip() for value in self.source_names.values()):
            raise ValueError("all GCP catalog sources required")
        for name, lower, upper in (("max_batch_records", 1, 10000), ("max_batch_bytes", 1, 64 * 1024 * 1024),
                                   ("max_message_bytes", 1, 10 * 1024 * 1024), ("max_receive_count", 1, 1000),
                                   ("ack_deadline_seconds", 30, 600)):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"invalid GCP collector limit: {name}")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GCPCollectorConfig:
        env = os.environ if env is None else env
        _reject_keys(env)
        catalog = load_catalog(env.get("MC_LOGAN_SOURCES_PATH") or None)
        try:
            projects = json.loads(env.get("GCP_ALLOWED_PROJECT_IDS", "[]"))
        except (ValueError, RecursionError):
            raise ValueError("GCP_ALLOWED_PROJECT_IDS must be a JSON array") from None
        if not isinstance(projects, list) or any(not isinstance(value, str) for value in projects):
            raise ValueError("GCP_ALLOWED_PROJECT_IDS must be a JSON string array")
        limits = {name: int(env[key]) for name, key in {
            "max_batch_records": "INGEST_MAX_BATCH_RECORDS", "max_batch_bytes": "INGEST_MAX_BATCH_BYTES",
            "max_message_bytes": "INGEST_MAX_MESSAGE_BYTES", "max_receive_count": "INGEST_MAX_RECEIVE_COUNT",
            "ack_deadline_seconds": "GCP_ACK_DEADLINE_SECONDS",
        }.items() if env.get(key)}
        return cls(namespace=env.get("LOGAN_NAMESPACE", ""), log_group_ocid=env.get("LOGAN_LOG_GROUP_OCID", ""),
                   subscription=env.get("GCP_PUBSUB_SUBSCRIPTION", ""), dlq_topic=env.get("GCP_PUBSUB_DLQ_TOPIC", ""),
                   allowed_project_ids=frozenset(projects),
                   dlq_directory=env.get("GCP_DLQ_DIRECTORY", ""),
                   source_names={key: catalog.source_name(key, tenant_id=env.get("MC_TENANT_ID")) for key in SOURCES},
                   oci_auth_mode=env.get("OCI_AUTH_MODE", "resource_principal"), oci_region=env.get("OCI_REGION", ""), **limits)


def _reject_keys(env: Mapping[str, str]) -> None:
    if any(env.get(key) for key in ("GOOGLE_APPLICATION_CREDENTIALS", "GCP_SERVICE_ACCOUNT_KEY")):
        raise ValueError("GCP default credentials and service account keys prohibited")


class GCPCollector:
    def __init__(self, config: GCPCollectorConfig, *, subscriber: Any, publisher: Any, logan: Any, poison_store: Any = None):
        self.config, self.subscriber, self.publisher = config, subscriber, publisher
        self.uploader = LoganUploader(config, logan)
        self.poison_store = poison_store if poison_store is not None else FilePoisonStore(config.dlq_directory)

    def _ack(self, ack_id: str) -> None:
        self.subscriber.acknowledge(request={"subscription": self.config.subscription, "ack_ids": [ack_id]}, timeout=30)

    @contextmanager
    def _lease(self, ack_id: str) -> Iterator[Event]:
        stopped, lost = Event(), Event()

        def renew() -> None:
            self.subscriber.modify_ack_deadline(request={"subscription": self.config.subscription,
                "ack_ids": [ack_id], "ack_deadline_seconds": self.config.ack_deadline_seconds}, timeout=10)

        def heartbeat() -> None:
            while not stopped.wait(self.config.ack_deadline_seconds / 3):
                try:
                    renew()
                except Exception:
                    lost.set()
                    logger.warning("ingest_ack_deadline_renewal_failed")
                    return

        renew()
        thread = Thread(target=heartbeat, name="gcp-ingest-lease", daemon=True)
        thread.start()
        try:
            yield lost
        finally:
            stopped.set()
            thread.join(timeout=15)
            if thread.is_alive():
                lost.set()

    def process_message(self, incoming: Any) -> str:
        ack_id = getattr(incoming, "ack_id", None)
        if not isinstance(ack_id, str) or not ack_id:
            logger.warning("ingest_receipt_missing")
            return "retry"
        try:
            with self._lease(ack_id) as lost:
                data = incoming.message.data
                message_id = incoming.message.message_id
                if not isinstance(data, bytes) or not isinstance(message_id, str) or not message_id:
                    raise RuntimeError("invalid Pub/Sub delivery envelope")
                identity = json.dumps([self.config.namespace, self.config.log_group_ocid, self.config.subscription, message_id])
                try:
                    if len(data) > self.config.max_message_bytes:
                        raise InvalidMessage("GCP message exceeds byte bound")
                    try:
                        record = json.loads(data.decode("utf-8"))
                    except (ValueError, UnicodeError, RecursionError):
                        raise InvalidMessage("invalid GCP LogEntry JSON") from None
                    key, row = normalize_gcp_record(record)
                    if row["cloud"]["account"]["uid"] not in self.config.allowed_project_ids:
                        raise InvalidMessage("GCP record outside collector scope")
                    try:
                        uploads = list(batch_ndjson([row], max_records=self.config.max_batch_records,
                                                    max_bytes=self.config.max_batch_bytes))
                    except UnicodeError:
                        raise InvalidMessage("invalid GCP field encoding") from None
                    if sum(map(len, uploads)) > self.config.max_message_bytes:
                        raise InvalidMessage("GCP normalized message exceeds bound")
                except InvalidMessage:
                    logger.warning("ingest_invalid_message")
                    digest = hashlib.sha256(identity.encode()).hexdigest()
                    envelope = {"schema_version": "mcingest.poison.v1", "reason": "invalid_message",
                                "body_sha256": hashlib.sha256(data).hexdigest(), "delivery_sha256": digest}
                    count = self.poison_store.record_failure(digest, envelope)
                    if count < self.config.max_receive_count:
                        return "retry"
                    envelope = {**envelope, "receive_count": count}
                    self.poison_store.send(digest, envelope)
                    if lost.is_set():
                        raise RuntimeError("GCP message lease lost")
                    published = self.publisher.publish(self.config.dlq_topic, json.dumps(envelope, sort_keys=True).encode()).result(timeout=30)
                    if not isinstance(published, str) or not published:
                        raise RuntimeError("GCP sanitized DLQ publish not accepted")
                    result = "dead_lettered"
                else:
                    for payload in uploads:
                        if lost.is_set():
                            raise RuntimeError("GCP message lease lost")
                        self.uploader.upload(payload, identity, source_name=self.config.source_names[key], filename_prefix=key.replace(".", "-"))
                    result = "uploaded"
            if lost.is_set():
                raise RuntimeError("GCP message lease lost")
            self._ack(ack_id)
            return result
        except Exception:
            logger.warning("ingest_provider_failure_retry")
            return "retry"

    def poll_once(self) -> dict[str, int]:
        counts = {"uploaded": 0, "retry": 0, "dead_lettered": 0}
        response = self.subscriber.pull(request={"subscription": self.config.subscription, "max_messages": 1}, timeout=30)
        for incoming in response.received_messages:
            counts[self.process_message(incoming)] += 1
        return counts


def build_gcp_runtime(config: GCPCollectorConfig, env: Mapping[str, str] | None = None) -> GCPCollector:
    """Explicit OIDC external-account credentials; no ADC or impersonation chain."""
    env = os.environ if env is None else env
    _reject_keys(env)
    provider, token_file = env.get("GCP_WIF_PROVIDER_RESOURCE", ""), env.get("GCP_WIF_TOKEN_FILE", "")
    if (not re.fullmatch(r"projects/[0-9]+/locations/global/workloadIdentityPools/[a-z0-9-]+/providers/[a-z0-9-]+", provider)
            or not token_file or not Path(token_file).is_absolute()):
        raise ValueError("explicit GCP federation provider and projected token required")
    from google.auth import identity_pool
    from google.cloud import pubsub_v1
    import oci
    from collectors.common.oci_auth import build_oci_client
    from ..runtime import suppress_sdk_payload_logging

    suppress_sdk_payload_logging()
    credential = identity_pool.Credentials.from_info({
        "type": "external_account", "audience": "//iam.googleapis.com/" + provider,
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt", "token_url": "https://sts.googleapis.com/v1/token",
        "credential_source": {"file": token_file, "format": {"type": "text"}},
    }, scopes=["https://www.googleapis.com/auth/pubsub"])
    subscriber = pubsub_v1.SubscriberClient(credentials=credential)
    publisher = None
    try:
        publisher = pubsub_v1.PublisherClient(credentials=credential)
        logan = build_oci_client(oci.log_analytics.LogAnalyticsClient, auth_mode=config.oci_auth_mode,
                                region=config.oci_region or None, timeout=(10, 30))
        return GCPCollector(config, subscriber=subscriber, publisher=publisher, logan=logan)
    except Exception:
        subscriber.close()
        if publisher is not None:
            publisher.stop()
        raise
