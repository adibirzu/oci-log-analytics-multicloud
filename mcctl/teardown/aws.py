"""AWS teardown: collector stop, trail+policy destroy, bucket empty, full destroy.

The CloudTrail bucket policy explicitly denies ``s3:DeleteObject``, so it must
be destroyed before the bucket can be emptied. ``force_destroy`` stays false.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from mcctl.teardown.base import (
    StepKind, TeardownContext, TeardownError, TeardownPlan, TeardownStep,
)

POLICY_TARGETS = ("aws_cloudtrail.this", "aws_s3_bucket_policy.cloudtrail")
BATCH = 1000
DENIED_HINT = "bucket policy still denies deletes — run the targeted policy destroy first"
STATIC_KEY_ENV = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")


class S3Client(Protocol):
    def list_object_versions(self, **kwargs: Any) -> dict[str, Any]: ...

    def delete_objects(self, **kwargs: Any) -> dict[str, Any]: ...


def make_s3_client(region: str, *, profile: str | None = None,
                   env: Mapping[str, str] | None = None) -> S3Client:
    """Lazy boto3 S3 client from the configured profile/region; static keys are refused."""
    env = os.environ if env is None else env
    if any(env.get(k) for k in STATIC_KEY_ENV):
        raise TeardownError("AWS static credential environment variables are prohibited; "
                            "use a profile or workload identity")
    import boto3  # lazy: only the real AWS teardown needs the SDK

    session = boto3.Session(profile_name=profile or env.get("AWS_PROFILE") or None, region_name=region)
    return session.client("s3")


def _code(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


def _delete(s3: S3Client, bucket: str, batch: list[dict[str, str]]) -> int:
    try:
        resp = s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
    except Exception as exc:  # noqa: BLE001 - re-raised with context
        if _code(exc) == "AccessDenied":
            raise TeardownError(f"AccessDenied emptying {bucket}: {DENIED_HINT}") from exc
        raise
    errors = resp.get("Errors") or []
    if errors:
        if any(e.get("Code") == "AccessDenied" for e in errors):
            raise TeardownError(f"AccessDenied emptying {bucket}: {DENIED_HINT}")
        raise TeardownError(f"failed deleting objects from {bucket}: {errors[:3]}")
    return len(batch)


def empty_bucket(s3: S3Client, bucket: str) -> int:
    """Delete every object version and delete marker; return the count."""
    deleted = 0
    batch: list[dict[str, str]] = []
    kwargs: dict[str, Any] = {"Bucket": bucket}
    while True:
        try:
            page = s3.list_object_versions(**kwargs)
        except Exception as exc:  # noqa: BLE001
            if _code(exc) == "AccessDenied":
                raise TeardownError(f"AccessDenied listing {bucket}: {DENIED_HINT}") from exc
            raise
        for item in [*page.get("Versions", ()), *page.get("DeleteMarkers", ())]:
            batch.append({"Key": item["Key"], "VersionId": item["VersionId"]})
            if len(batch) >= BATCH:
                deleted += _delete(s3, bucket, batch)
                batch = []
        if not page.get("IsTruncated"):
            break
        kwargs["KeyMarker"] = page.get("NextKeyMarker")
        kwargs["VersionIdMarker"] = page.get("NextVersionIdMarker")
    if batch:
        deleted += _delete(s3, bucket, batch)
    return deleted


def build_plan(*, stop_collector: Callable[[], None] | None, s3: S3Client,
               bucket_name: str | None = None) -> TeardownPlan:
    def _stop(_ctx: TeardownContext) -> None:
        if stop_collector is not None:
            stop_collector()

    def _empty(ctx: TeardownContext) -> None:
        bucket = bucket_name or ctx.outputs.get("bucket_name")
        if not bucket:
            raise TeardownError("bucket_name not found in terraform outputs")
        empty_bucket(s3, str(bucket))

    return TeardownPlan("aws", (
        TeardownStep(StepKind.COLLECTOR_STOP, "stop the collector", action=_stop),
        TeardownStep(StepKind.TARGETED_DESTROY, "destroy CloudTrail trail and bucket policy",
                     targets=POLICY_TARGETS, strict=True),
        TeardownStep(StepKind.EMPTY_BUCKET, "empty CloudTrail bucket (versions and delete markers)",
                     action=_empty),
        TeardownStep(StepKind.DESTROY, "full destroy"),
        TeardownStep(StepKind.VERIFY, "verify state is empty"),
    ))
