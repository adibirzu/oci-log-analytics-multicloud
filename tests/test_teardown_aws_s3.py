"""AWS teardown: real empty-bucket path against a hand-rolled fake S3 client (no moto, no network)."""
import sys
import types

import pytest

from mcctl.teardown import aws
from mcctl.teardown.base import TeardownError

BUCKET = "cloudtrail-bucket-fixture"


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """Emulates list_object_versions paging (KeyMarker/VersionIdMarker) and delete_objects batches."""

    def __init__(self, entries, page_size=300, delete_errors=None, list_error=None):
        self.entries = entries  # [{"kind": "v"|"m", "Key": str, "VersionId": str}]
        self.index = {(e["Key"], e["VersionId"]): i for i, e in enumerate(entries)}
        self.page_size = page_size
        self.delete_errors = delete_errors
        self.list_error = list_error
        self.list_calls, self.delete_calls, self.deleted = [], [], []

    def list_object_versions(self, **kwargs):
        if self.list_error:
            raise self.list_error
        assert kwargs["Bucket"] == BUCKET
        self.list_calls.append({k: v for k, v in kwargs.items() if k != "Bucket"})
        marker = (kwargs.get("KeyMarker"), kwargs.get("VersionIdMarker"))
        start = self.index[marker] + 1 if kwargs.get("KeyMarker") is not None else 0
        chunk = self.entries[start:start + self.page_size]
        truncated = start + self.page_size < len(self.entries)
        page = {
            "Versions": [{"Key": e["Key"], "VersionId": e["VersionId"]} for e in chunk if e["kind"] == "v"],
            "DeleteMarkers": [{"Key": e["Key"], "VersionId": e["VersionId"]} for e in chunk if e["kind"] == "m"],
            "IsTruncated": truncated,
        }
        if truncated:
            page["NextKeyMarker"] = chunk[-1]["Key"]
            page["NextVersionIdMarker"] = chunk[-1]["VersionId"]
        return page

    def delete_objects(self, **kwargs):
        assert kwargs["Bucket"] == BUCKET and kwargs["Delete"]["Quiet"] is True
        batch = kwargs["Delete"]["Objects"]
        self.delete_calls.append(len(batch))
        if self.delete_errors is not None:
            return {"Errors": self.delete_errors}
        self.deleted.extend((o["Key"], o["VersionId"]) for o in batch)
        return {}


def make_entries(versions, markers):
    out = [{"kind": "v", "Key": f"logs/{i:05d}.json.gz", "VersionId": f"v{i}"} for i in range(versions)]
    out += [{"kind": "m", "Key": f"logs/{i:05d}.json.gz", "VersionId": f"m{i}"} for i in range(markers)]
    return out


def test_empty_bucket_deletes_all_versions_and_markers_in_batches_of_1000():
    entries = make_entries(versions=1800, markers=700)  # 2500 entries across several list pages
    s3 = FakeS3(entries, page_size=300)

    deleted = aws.empty_bucket(s3, BUCKET)

    assert deleted == 2500
    assert max(s3.delete_calls) <= aws.BATCH == 1000
    assert s3.delete_calls == [1000, 1000, 500]
    assert sorted(s3.deleted) == sorted((e["Key"], e["VersionId"]) for e in entries)
    assert len(s3.deleted) == len(set(s3.deleted))


def test_listing_follows_key_and_version_markers():
    entries = make_entries(versions=5, markers=2)
    s3 = FakeS3(entries, page_size=3)

    aws.empty_bucket(s3, BUCKET)

    assert s3.list_calls[0] == {}
    assert len(s3.list_calls) == 3
    second = s3.list_calls[1]
    assert second == {"KeyMarker": entries[2]["Key"], "VersionIdMarker": entries[2]["VersionId"]}
    third = s3.list_calls[2]
    assert third == {"KeyMarker": entries[5]["Key"], "VersionIdMarker": entries[5]["VersionId"]}


def test_empty_bucket_with_no_objects_makes_no_delete_calls():
    s3 = FakeS3([])
    assert aws.empty_bucket(s3, BUCKET) == 0
    assert s3.delete_calls == []


def test_access_denied_in_delete_errors_raises_policy_message():
    s3 = FakeS3(make_entries(versions=3, markers=0),
                delete_errors=[{"Key": "logs/00000.json.gz", "Code": "AccessDenied", "Message": "denied"}])
    with pytest.raises(TeardownError, match="bucket policy still denies deletes"):
        aws.empty_bucket(s3, BUCKET)


def test_access_denied_raised_by_sdk_on_delete_gets_policy_message():
    s3 = FakeS3(make_entries(versions=1, markers=0))

    def denied(**_):
        raise ClientError("AccessDenied")

    s3.delete_objects = denied
    with pytest.raises(TeardownError, match="run the targeted policy destroy first"):
        aws.empty_bucket(s3, BUCKET)


def test_access_denied_on_list_gets_policy_message():
    s3 = FakeS3([], list_error=ClientError("AccessDenied"))
    with pytest.raises(TeardownError, match="AccessDenied listing"):
        aws.empty_bucket(s3, BUCKET)


def test_other_delete_errors_are_not_mislabelled():
    s3 = FakeS3(make_entries(versions=1, markers=0),
                delete_errors=[{"Key": "k", "Code": "InternalError", "Message": "boom"}])
    with pytest.raises(TeardownError, match="failed deleting objects") as exc:
        aws.empty_bucket(s3, BUCKET)
    assert "bucket policy" not in str(exc.value)


# --- lazy boto3 client builder -------------------------------------------------

class RecordingEnv(dict):
    def __init__(self, data):
        super().__init__(data)
        self.reads = []

    def get(self, key, default=None):
        self.reads.append(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.reads.append(key)
        return super().__getitem__(key)


@pytest.fixture
def fake_boto3(monkeypatch):
    sessions = []

    class Session:
        def __init__(self, profile_name=None, region_name=None):
            self.kwargs = {"profile_name": profile_name, "region_name": region_name}
            sessions.append(self)

        def client(self, service):
            return ("client", service, self.kwargs)

    module = types.ModuleType("boto3")
    module.Session = Session
    monkeypatch.setitem(sys.modules, "boto3", module)
    return sessions


def test_client_builder_uses_configured_profile_and_region_without_static_keys(fake_boto3):
    env = RecordingEnv({"AWS_PROFILE": "mcctl-env-profile"})

    client = aws.make_s3_client("eu-central-1", profile="mcctl-prod", env=env)

    assert client == ("client", "s3", {"profile_name": "mcctl-prod", "region_name": "eu-central-1"})
    assert len(fake_boto3) == 1
    # Static keys are only checked to refuse them; they never reach the session.
    assert set(env.reads) <= set(aws.STATIC_KEY_ENV) | {"AWS_PROFILE"}
    assert set(fake_boto3[0].kwargs) == {"profile_name", "region_name"}


def test_client_builder_falls_back_to_aws_profile_env(fake_boto3):
    client = aws.make_s3_client("eu-west-1", env={"AWS_PROFILE": "mcctl-env-profile"})
    assert client[2] == {"profile_name": "mcctl-env-profile", "region_name": "eu-west-1"}


@pytest.mark.parametrize("key", aws.STATIC_KEY_ENV)
def test_client_builder_refuses_static_keys(fake_boto3, key):
    with pytest.raises(TeardownError, match="static credential"):
        aws.make_s3_client("eu-central-1", env={key: "x"})
    assert fake_boto3 == [], "no session may be created when static keys are present"
