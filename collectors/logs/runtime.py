"""Lazy, keyless SDK construction with refreshable assumed-role credentials."""

from __future__ import annotations

import logging
from typing import Any

from .config import CollectorConfig
from .pipeline import Collector


def suppress_sdk_payload_logging() -> None:
    """SDK DEBUG logs may include signed headers, tokens and upload bodies."""
    for name in ("boto3", "botocore", "oci", "urllib3", "requests", "azure", "uamqp", "google", "grpc"):
        logging.getLogger(name).setLevel(logging.CRITICAL + 1)
    # Existing child loggers can have explicitly enabled DEBUG levels.
    for name, candidate in logging.Logger.manager.loggerDict.copy().items():
        if isinstance(candidate, logging.Logger) and name.startswith(("boto3.", "botocore.", "oci.", "urllib3.", "requests.", "azure.", "uamqp.", "google.", "grpc.")):
            candidate.setLevel(logging.CRITICAL + 1)


def build_aws_session(config: CollectorConfig) -> Any:
    import boto3
    import botocore.session
    from botocore.config import Config
    from botocore.credentials import (
        AssumeRoleCredentialFetcher,
        AssumeRoleWithWebIdentityCredentialFetcher,
        DeferredRefreshableCredentials,
    )
    from botocore.utils import FileWebIdentityTokenLoader

    suppress_sdk_payload_logging()
    base = botocore.session.get_session()
    base.set_config_variable("region", config.aws_region)
    sdk_config = Config(connect_timeout=10, read_timeout=30, retries={"mode": "standard", "max_attempts": 3})

    def create_sts(service: str, **kwargs: Any) -> Any:
        kwargs["config"] = sdk_config.merge(kwargs["config"]) if "config" in kwargs else sdk_config
        return base.create_client(service, region_name=config.aws_region, **kwargs)

    extra = {"RoleSessionName": config.role_session_name}
    if config.aws_auth_mode == "web_identity":
        fetcher = AssumeRoleWithWebIdentityCredentialFetcher(
            client_creator=create_sts, web_identity_token_loader=FileWebIdentityTokenLoader(config.web_identity_token_file),
            role_arn=config.aws_role_arn, extra_args=extra,
        )
        method = "assume-role-with-web-identity"
    else:
        credentials = base.get_credentials()
        methods = {"iam-role", "container-role", "assume-role-with-web-identity", "sso"}
        if config.allow_operator_auth:
            methods = methods | {"assume-role"}
        if (credentials is None or credentials.method not in methods
                or not credentials.get_frozen_credentials().token):
            raise ValueError("external-ID mode requires an existing temporary workload principal")
        fetcher = AssumeRoleCredentialFetcher(client_creator=create_sts, source_credentials=credentials,
                                              role_arn=config.aws_role_arn, extra_args={**extra, "ExternalId": config.external_id})
        method = "assume-role"
    session = botocore.session.get_session()
    session.set_config_variable("region", config.aws_region)
    session._credentials = DeferredRefreshableCredentials(refresh_using=fetcher.fetch_credentials, method=method)
    return boto3.Session(botocore_session=session)


def build_collector(config: CollectorConfig) -> Collector:
    import oci
    from botocore.config import Config
    from collectors.common.oci_auth import build_oci_client

    suppress_sdk_payload_logging()
    aws = build_aws_session(config)
    sdk_config = Config(connect_timeout=10, read_timeout=30, retries={"mode": "standard", "max_attempts": 3})
    return Collector(
        config, sqs=aws.client("sqs", config=sdk_config), s3=aws.client("s3", config=sdk_config),
        logan=build_oci_client(oci.log_analytics.LogAnalyticsClient, auth_mode=config.oci_auth_mode,
                              region=config.oci_region or None, timeout=(10, 30)),
    )
