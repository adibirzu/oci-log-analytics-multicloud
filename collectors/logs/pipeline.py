"""At-least-once queue ingestion. No payloads or provider errors enter logs."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import re
from datetime import datetime
from contextlib import contextmanager
from threading import Event, Thread
from typing import Any, Iterator, Mapping
from urllib.parse import unquote_plus

from .config import CollectorConfig
from .upload import LoganUploader
from .normalize import InvalidMessage, batch_ndjson, normalize_record

logger = logging.getLogger("mc.Ingest.AWS")

INVALID_STAGES = ("notification", "scope", "object_decode", "normalize", "limits")
REASONS = ("s3_test_event", "digest_only", "uploaded", "retry_provider_failure")


def _invalid(text: str, stage: str) -> InvalidMessage:
    exc = InvalidMessage(text)
    exc.stage = stage
    return exc


def _stage(exc: BaseException) -> str:
    stage = getattr(exc, "stage", "")
    return stage if stage in INVALID_STAGES else "notification"


class Collector:
    """Clients expose the standard boto3 SQS/S3 and OCI upload SDK methods."""

    def __init__(self, config: CollectorConfig, *, sqs: Any, s3: Any, logan: Any):
        self.config, self.sqs, self.s3, self.logan = config, sqs, s3, logan
        self.uploader = LoganUploader(config, logan)

    def _references(self, body: str) -> list[tuple[str, str]]:
        return self._scan(body)[0]

    def _digest_pattern(self) -> "re.Pattern[str]":
        return re.compile(re.escape(self.config.prefix) + r"/AWSLogs/" + re.escape(self.config.account_id)
                          + r"/CloudTrail-Digest/[a-z]{2}(?:-[a-z]+)+-\d+/\d{4}/\d{2}/\d{2}/[^/]+\.json\.gz")

    def _scan(self, body: str) -> tuple[list[tuple[str, str]], int, bool]:
        """Return (data references, digest refs skipped, is S3 test event)."""
        if not isinstance(body, str) or len(body.encode("utf-8")) > 1024 * 1024:
            raise _invalid("invalid notification body", "notification")
        try:
            document = json.loads(body)
        except (ValueError, RecursionError):
            raise _invalid("notification is not JSON", "notification") from None
        if not isinstance(document, Mapping):
            raise _invalid("invalid notification", "notification")
        if document.get("Event") == "s3:TestEvent" and document.get("Bucket") == self.config.bucket:
            return [], 0, True
        records = document.get("Records")
        if not isinstance(records, list) or not 1 <= len(records) <= 100:
            raise _invalid("notification has no bounded records", "notification")
        references: list[tuple[str, str]] = []
        digest_skipped = 0
        digest_re = self._digest_pattern()
        for record in records:
            try:
                bucket = record["s3"]["bucket"]["name"]
                key = unquote_plus(record["s3"]["object"]["key"])
                event = record["eventName"]
                if (record["eventSource"] != "aws:s3" or record["awsRegion"] != self.config.aws_region
                        or not isinstance(event, str) or not event.startswith("ObjectCreated:")
                        or bucket != self.config.bucket or not key.endswith(".gz")
                        or not key.startswith(f"{self.config.prefix}/AWSLogs/{self.config.account_id}/")
                        or any(part in {".", ".."} for part in key.split("/"))):
                    raise _invalid("notification scope mismatch", "scope")
            except (KeyError, TypeError, AttributeError):
                raise _invalid("invalid object reference", "scope") from None
            if digest_re.fullmatch(key):
                digest_skipped += 1
                continue
            relative = key[len(f"{self.config.prefix}/AWSLogs/{self.config.account_id}/"):]
            pattern = (r"CloudTrail/([a-z]{2}(?:-[a-z]+)+-\d+)/(\d{4})/(\d{2})/(\d{2})/"
                       + re.escape(self.config.account_id) + r"_CloudTrail_([^_]+)_(\d{8})T\d{4}Z_[A-Za-z0-9]+\.json\.gz")
            match = re.fullmatch(pattern, relative)
            if not match or match[1] != match[5] or "".join(match.group(2, 3, 4)) != match[6]:
                raise _invalid("noncanonical CloudTrail object", "scope")
            try:
                datetime.strptime(match[6], "%Y%m%d")
            except ValueError:
                raise _invalid("invalid object date", "scope") from None
            if (bucket, key) not in references:
                references.append((bucket, key))
        return references, digest_skipped, False

    def _read_records(self, bucket: str, key: str, remaining_bytes: int) -> tuple[list[dict[str, Any]], int]:
        response = self.s3.get_object(Bucket=bucket, Key=key, ExpectedBucketOwner=self.config.account_id)
        body = response["Body"]
        try:
            size = response.get("ContentLength")
            if isinstance(size, int) and size > self.config.max_object_bytes:
                raise _invalid("compressed object exceeds size bound", "limits")
            compressed = body.read(self.config.max_object_bytes + 1)
        finally:
            body.close()
        if len(compressed) > self.config.max_object_bytes:
            raise _invalid("compressed object exceeds size bound", "limits")
        try:
            bound = min(self.config.max_object_bytes, remaining_bytes)
            with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
                raw = stream.read(bound + 1)
            if len(raw) > bound:
                raise _invalid("expanded object exceeds size bound", "limits")
            document = json.loads(raw)
        except (OSError, EOFError, ValueError, RecursionError):
            raise _invalid("invalid gzip CloudTrail JSON", "object_decode") from None
        records = document.get("Records") if isinstance(document, Mapping) else None
        if not isinstance(records, list) or not 1 <= len(records) <= self.config.max_records_per_object:
            raise _invalid("CloudTrail object has no bounded records", "limits")
        try:
            normalized = [normalize_record(record) for record in records]
        except InvalidMessage as exc:
            exc.stage = getattr(exc, "stage", "normalize")
            raise
        if any(record["cloud"]["account"]["uid"] != self.config.account_id for record in normalized):
            raise _invalid("record account outside collector scope", "normalize")
        return normalized, len(raw)

    @contextmanager
    def _lease(self, receipt: str) -> Iterator[Event]:
        """Renew visibility for a single in-flight message; fail closed on loss."""
        stopped, lost = Event(), Event()

        def renew() -> None:
            self.sqs.change_message_visibility(QueueUrl=self.config.queue_url, ReceiptHandle=receipt,
                                               VisibilityTimeout=self.config.visibility_seconds)

        def heartbeat() -> None:
            while not stopped.wait(self.config.visibility_seconds / 3):
                try:
                    renew()
                except Exception:
                    lost.set()
                    logger.warning("ingest_visibility_renewal_failed")
                    return

        renew()
        thread = Thread(target=heartbeat, name="ingest-visibility", daemon=True)
        thread.start()
        try:
            yield lost
        finally:
            stopped.set()
            thread.join(timeout=35)
            if thread.is_alive():
                lost.set()

    def _upload(self, payload: bytes, identity: str) -> None:
        self.uploader.upload(payload, identity, source_name=self.config.source_name, filename_prefix="aws-cloudtrail")

    def _poison(self, message: Mapping[str, Any], stage: str = "notification") -> tuple[str, str]:
        logger.warning("ingest_invalid_message")
        try:
            count = int(message.get("Attributes", {}).get("ApproximateReceiveCount", "0"))
            if count < self.config.max_receive_count:
                return "retry", f"retry_invalid:{stage}"
            body = message.get("Body", "")
            # The original remains replayable via S3; arbitrary raw bodies can
            # contain credentials. Retain only checked scope references and a digest.
            try:
                references = self._references(body)
            except InvalidMessage:
                references = []
            envelope = {"schema_version": "mcingest.poison.v1", "reason": "invalid_message",
                        "receive_count": count, "body_sha256": hashlib.sha256(str(body).encode()).hexdigest(),
                        "objects": [{"bucket": bucket, "key": key} for bucket, key in references]}
            response = self.sqs.send_message(QueueUrl=self.config.dlq_url, MessageBody=json.dumps(envelope))
            if not response.get("MessageId"):
                raise RuntimeError("DLQ send not accepted")
            self.sqs.delete_message(QueueUrl=self.config.queue_url, ReceiptHandle=message["ReceiptHandle"])
            return "dead_lettered", f"dlq_invalid:{stage}"
        except Exception:
            logger.warning("ingest_poison_transfer_failed")
            return "retry", "retry_provider_failure"

    def process_message(self, message: Mapping[str, Any]) -> str:
        """Acknowledge after every upload, or a verified DLQ send for poison."""
        return self.process_message_detail(message)[0]

    def process_message_detail(self, message: Mapping[str, Any]) -> tuple[str, str, dict[str, int]]:
        """Return (outcome, reason code, safe counts); logs one structured line."""
        stats = {"references": 0, "digest_skipped": 0, "records": 0, "batches_uploaded": 0}
        outcome, reason = self._process(message, stats)
        logger.info("ingest_message_result outcome=%s reason=%s references=%d digest_skipped=%d records=%d "
                    "batches_uploaded=%d", outcome, reason, stats["references"], stats["digest_skipped"],
                    stats["records"], stats["batches_uploaded"])
        return outcome, reason, stats

    def _process(self, message: Mapping[str, Any], stats: dict[str, int]) -> tuple[str, str]:
        receipt = message.get("ReceiptHandle")
        if not isinstance(receipt, str) or not receipt:
            logger.warning("ingest_receipt_missing")
            return "retry", "retry_provider_failure"
        try:
            with self._lease(receipt) as lost:
                references, digest_skipped, test_event = self._scan(message.get("Body"))
                stats["references"], stats["digest_skipped"] = len(references), digest_skipped
                uploads: list[tuple[bytes, str]] = []
                total, expanded, record_count = 0, 0, 0
                # Validate all referenced objects and all records before uploading.
                for bucket, key in references:
                    rows, decoded_bytes = self._read_records(bucket, key, self.config.max_message_bytes - expanded)
                    expanded += decoded_bytes
                    record_count += len(rows)
                    if record_count > self.config.max_records_per_message:
                        raise _invalid("message exceeds record count bound", "limits")
                    identity = json.dumps([self.config.namespace, self.config.log_group_ocid,
                                           self.config.source_name, bucket, key])
                    for payload in batch_ndjson(rows, max_records=self.config.max_batch_records,
                                                max_bytes=self.config.max_batch_bytes):
                        total += len(payload)
                        if total > self.config.max_message_bytes:
                            raise _invalid("message exceeds aggregate upload bound", "limits")
                        uploads.append((payload, identity))
                stats["records"] = record_count
                for payload, identity in uploads:
                    if lost.is_set():
                        raise RuntimeError("message visibility lost")
                    self._upload(payload, identity)
                    stats["batches_uploaded"] += 1
            if lost.is_set():
                raise RuntimeError("message visibility lost")
            self.sqs.delete_message(QueueUrl=self.config.queue_url, ReceiptHandle=receipt)
            if uploads:
                return "uploaded", "uploaded"
            return "ignored", "s3_test_event" if test_event else "digest_only"
        except InvalidMessage as exc:
            return self._poison(message, _stage(exc))
        except Exception:
            # SDK messages/tracebacks can contain signed headers and request bodies.
            logger.warning("ingest_provider_failure_retry")
            return "retry", "retry_provider_failure"

    def poll_once(self) -> dict[str, int]:
        """One message per lease prevents visibility expiry while others wait."""
        response = self.sqs.receive_message(
            QueueUrl=self.config.queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=self.config.wait_seconds,
            VisibilityTimeout=self.config.visibility_seconds,
            MessageSystemAttributeNames=["ApproximateReceiveCount"],
        )
        counts = {"uploaded": 0, "ignored": 0, "retry": 0, "dead_lettered": 0}
        for message in response.get("Messages", []):
            outcome, reason, _ = self.process_message_detail(message)
            counts[outcome] += 1
            key = f"reason.{reason}"
            counts[key] = counts.get(key, 0) + 1
        return counts
