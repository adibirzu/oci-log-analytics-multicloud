import pytest

from fakes_mcctl import FakeRunner, load
from mcctl.plan import StalePlanError, apply_saved, sha256_file, summarize
from mcctl.state import Layout, Manifest, write_manifest


def test_summarize_enable_plan():
    s = summarize(load("show_enable.json"))
    assert (s.add, s.change, s.destroy, s.replace) == (3, 1, 0, 0)
    assert s.iam_changes == ["create aws_iam_role.ingest", "create aws_iam_role_policy.ingest"]
    assert s.destroys == [] and not s.is_empty


def test_summarize_noop_is_empty():
    assert summarize(load("show_noop.json")).is_empty


def test_summarize_replace_and_removal():
    s = summarize(load("show_replace.json"))
    assert (s.add, s.destroy, s.replace) == (0, 1, 2)
    assert s.destroys == ["aws_s3_bucket.logs", "aws_sqs_queue.q", "aws_iam_role.old"]
    assert "delete aws_iam_role.old" in s.iam_changes


def _prepared(tmp_path, serial=1):
    lay = Layout(tmp_path, "aws", "111122223333", "logs")
    lay.ensure()
    r = FakeRunner(load("show_enable.json"), serial=serial)
    r.plan(lay.plan_path)
    write_manifest(lay, Manifest(str(lay.plan_path), sha256_file(lay.plan_path), r.state_serial(), {}))
    return lay, r


def test_apply_saved_happy_path(tmp_path):
    lay, r = _prepared(tmp_path)
    apply_saved(r, lay)
    assert "apply" in r.calls


def test_apply_refuses_modified_plan(tmp_path):
    lay, r = _prepared(tmp_path)
    lay.plan_path.write_bytes(b"tampered")
    with pytest.raises(StalePlanError, match="sha256"):
        apply_saved(r, lay)
    assert "apply" not in r.calls


def test_apply_refuses_stale_serial(tmp_path):
    lay, r = _prepared(tmp_path)
    r.serial += 1
    with pytest.raises(StalePlanError, match="serial"):
        apply_saved(r, lay)
    assert "apply" not in r.calls


def test_apply_refuses_without_manifest(tmp_path):
    lay = Layout(tmp_path, "aws", "111122223333", "logs")
    with pytest.raises(StalePlanError):
        apply_saved(FakeRunner({}), lay)
