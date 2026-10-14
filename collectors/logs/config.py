"""Explicit per-collector configuration, with no deployment identities baked in."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlparse

from .catalog import load_catalog


@dataclass(frozen=True, repr=False)
class CollectorConfig:
    namespace: str
    log_group_ocid: str
    source_name: str
    queue_url: str
    dlq_url: str
    bucket: str
    prefix: str
    account_id: str
    aws_region: str
    aws_role_arn: str
    aws_auth_mode: str = "web_identity"
    web_identity_token_file: str = field(default="", repr=False)
    external_id: str = field(default="", repr=False)
    role_session_name: str = "mc-cloudtrail"
    oci_auth_mode: str = "resource_principal"
    oci_region: str = ""
    # Operator test runs only: admits OCI api_key and AWS assume-role base credentials.
    allow_operator_auth: bool = False
    max_batch_records: int = 500
    max_batch_bytes: int = 4 * 1024 * 1024
    max_object_bytes: int = 8 * 1024 * 1024
    max_message_bytes: int = 64 * 1024 * 1024
    max_records_per_object: int = 10000
    max_records_per_message: int = 20000
    max_receive_count: int = 5
    wait_seconds: int = 20
    visibility_seconds: int = 300

    def __post_init__(self) -> None:
        for name in ("namespace", "source_name", "log_group_ocid", "bucket", "prefix", "aws_role_arn"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"missing collector setting: {name}")
        if not self.log_group_ocid.startswith("ocid1.loganalyticsloggroup."):
            raise ValueError("log_group_ocid must identify a Log Analytics log group")
        if not re.fullmatch(r"\d{12}", self.account_id):
            raise ValueError("account_id must be a 12-digit AWS account ID")
        if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", self.aws_region):
            raise ValueError("aws_region is required")
        if not re.fullmatch(rf"arn:(aws|aws-us-gov|aws-cn):iam::{self.account_id}:role/[\w+=,.@/-]+", self.aws_role_arn):
            raise ValueError("aws_role_arn must be an exact role in the configured account")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.bucket):
            raise ValueError("invalid bucket name")
        if self.prefix.startswith("/") or any(part in {"", ".", ".."} for part in self.prefix.split("/")):
            raise ValueError("prefix must be an exact nonempty S3 prefix without traversal")
        if self.queue_url == self.dlq_url:
            raise ValueError("source queue and DLQ must differ")
        suffix = "amazonaws.com.cn" if self.aws_role_arn.startswith("arn:aws-cn:") else "amazonaws.com"
        for url in (self.queue_url, self.dlq_url):
            parsed = urlparse(url)
            if (parsed.scheme != "https" or parsed.hostname != f"sqs.{self.aws_region}.{suffix}"
                    or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment
                    or not re.fullmatch(rf"/{self.account_id}/[A-Za-z0-9_-]+", parsed.path)):
                raise ValueError("queue URLs must be exact same-region standard queues in the configured account")
        if self.aws_auth_mode == "web_identity":
            if not self.web_identity_token_file or self.external_id:
                raise ValueError("web_identity requires a token file and no external ID")
        elif self.aws_auth_mode == "external_id":
            if not self.external_id or self.web_identity_token_file:
                raise ValueError("external_id mode requires an external ID and no web token file")
        else:
            raise ValueError("unsupported AWS auth mode")
        if self.oci_auth_mode not in allowed_oci_auth_modes(self.allow_operator_auth):
            raise ValueError("OCI ingestion requires instance or resource principal authentication")
        if not re.fullmatch(r"[\w+=,.@-]{2,64}", self.role_session_name):
            raise ValueError("invalid role session name")
        limits = {"max_batch_records": (1, 10000), "max_batch_bytes": (1, 64 * 1024 * 1024),
                  "max_object_bytes": (1, 32 * 1024 * 1024), "max_message_bytes": (1, 256 * 1024 * 1024),
                  "max_records_per_object": (1, 100000), "max_records_per_message": (1, 100000),
                  "max_receive_count": (1, 1000), "wait_seconds": (0, 20), "visibility_seconds": (30, 43200)}
        for name, (low, high) in limits.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"collector limit out of range: {name}")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> CollectorConfig:
        env = os.environ if env is None else env
        if any(env.get(key) for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")):
            raise ValueError("AWS environment credentials are prohibited; use workload identity")

        def value(key: str, default: str = "") -> str:
            return env.get(key, default).strip()

        catalog = load_catalog(value("MC_LOGAN_SOURCES_PATH") or None)
        default_source = catalog.source_name("aws.cloudtrail", tenant_id=value("MC_TENANT_ID") or None)
        numeric = {"max_batch_records": "INGEST_MAX_BATCH_RECORDS", "max_batch_bytes": "INGEST_MAX_BATCH_BYTES",
                   "max_object_bytes": "INGEST_MAX_OBJECT_BYTES", "max_message_bytes": "INGEST_MAX_MESSAGE_BYTES",
                   "max_records_per_object": "INGEST_MAX_RECORDS_PER_OBJECT", "max_records_per_message": "INGEST_MAX_RECORDS_PER_MESSAGE",
                   "max_receive_count": "INGEST_MAX_RECEIVE_COUNT", "wait_seconds": "INGEST_WAIT_SECONDS",
                   "visibility_seconds": "INGEST_VISIBILITY_SECONDS"}
        numbers = {}
        for name, key in numeric.items():
            if value(key):
                try:
                    numbers[name] = int(value(key))
                except ValueError:
                    raise ValueError(f"collector setting must be an integer: {key}") from None
        return cls(
            namespace=value("LOGAN_NAMESPACE"), log_group_ocid=value("LOGAN_LOG_GROUP_OCID"),
            source_name=value("LOGAN_SOURCE_NAME", default_source), queue_url=value("AWS_SQS_QUEUE_URL"),
            dlq_url=value("AWS_SQS_DLQ_URL"), bucket=value("AWS_S3_BUCKET"), prefix=value("AWS_S3_PREFIX"),
            account_id=value("AWS_ACCOUNT_ID"), aws_region=value("AWS_REGION"), aws_role_arn=value("AWS_ROLE_ARN"),
            aws_auth_mode=value("AWS_AUTH_MODE", "web_identity"),
            web_identity_token_file=value("AWS_WEB_IDENTITY_TOKEN_FILE"), external_id=value("AWS_EXTERNAL_ID"),
            role_session_name=value("AWS_ROLE_SESSION_NAME", "mc-cloudtrail"),
            oci_auth_mode=value("OCI_AUTH_MODE", "resource_principal"), oci_region=value("OCI_REGION"),
            allow_operator_auth=operator_auth_enabled(env), **numbers,
        )


WORKLOAD_OCI_AUTH_MODES = frozenset({"instance_principal", "resource_principal"})


def operator_auth_enabled(env: Mapping[str, str]) -> bool:
    """Explicit opt-in for a human operator's credentials during a supervised test run."""
    return env.get("INGEST_ALLOW_OPERATOR_AUTH", "").strip().lower() == "true"


def allowed_oci_auth_modes(allow_operator_auth: bool) -> frozenset[str]:
    return WORKLOAD_OCI_AUTH_MODES | {"api_key"} if allow_operator_auth else WORKLOAD_OCI_AUTH_MODES
