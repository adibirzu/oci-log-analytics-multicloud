"""Teardown engine tests with fake terraform runner and fake S3."""

import pytest

from mcctl.teardown import aws, azure, base, gcp
from mcctl.teardown.base import (
    ConfirmationError, PlanReviewError, StepKind, TeardownContext, TeardownError, run,
)

ACCT = "<ACCOUNT_ID>"


def change(addr, action="delete"):
    return {"address": addr, "change": {"actions": [action]}}


class FakeRunner:
    def __init__(self, targeted=None, full=(), outputs=None, left_after=()):
        self.calls, self.targeted, self.full = [], targeted or {}, list(full)
        self.outputs_map, self.left_after = outputs or {}, list(left_after)
        self._plans, self.applied = {}, 0

    def plan(self, out, destroy, targets, var_file):
        self.calls.append(("plan", tuple(targets)))
        self._plans[out] = tuple(targets)

    def show_json(self, plan):
        t = self._plans[plan]
        if t:
            rcs = self.targeted.get(t, [change(a) for a in t])
        elif self.applied >= self.n_destroys:
            rcs = []
        else:
            rcs = self.full
        out = {k: {"value": v} for k, v in self.outputs_map.items()}
        pj = {"resource_changes": rcs, "prior_state": {"values": {"outputs": out}}}
        if not t and self.applied >= self.n_destroys:
            pj["prior_state"]["values"]["root_module"] = {
                "resources": [{"address": a} for a in self.left_after]}
        return pj

    n_destroys = 99

    def outputs(self):
        return {k: v for k, v in self.outputs_map.items()}

    def apply(self, plan):
        self.calls.append(("apply", self._plans[plan]))
        self.applied += 1


class FakeS3:
    def __init__(self, versions=0, markers=0, deny=False):
        self.objs = [{"Key": f"k{i}", "VersionId": f"v{i}"} for i in range(versions)]
        self.marks = [{"Key": f"m{i}", "VersionId": f"d{i}"} for i in range(markers)]
        self.deny, self.delete_sizes = deny, []
        self.snapshot = [("V", o) for o in self.objs] + [("M", o) for o in self.marks]

    def list_object_versions(self, Bucket, KeyMarker=None, VersionIdMarker=None):
        start = int(KeyMarker or 0)
        allv = self.snapshot
        page = allv[start:start + 1000]
        more = start + 1000 < len(allv)
        return {"Versions": [o for t, o in page if t == "V"],
                "DeleteMarkers": [o for t, o in page if t == "M"],
                "IsTruncated": more, "NextKeyMarker": str(start + 1000),
                "NextVersionIdMarker": "x"}

    def delete_objects(self, Bucket, Delete):
        objs = Delete["Objects"]
        self.delete_sizes.append(len(objs))
        if self.deny:
            return {"Errors": [{"Key": objs[0]["Key"], "Code": "AccessDenied"}]}
        gone = {(o["Key"], o["VersionId"]) for o in objs}
        self.objs = [o for o in self.objs if (o["Key"], o["VersionId"]) not in gone]
        self.marks = [o for o in self.marks if (o["Key"], o["VersionId"]) not in gone]
        return {}


def ctx(runner, tmp_path):
    return TeardownContext(runner=runner, workdir=tmp_path)


def aws_plan(s3, events):
    return aws.build_plan(stop_collector=lambda: events.append("stop"), s3=s3)


def test_aws_order_and_empty(tmp_path):
    events, s3 = [], FakeS3(versions=2500, markers=300)
    r = FakeRunner(outputs={"bucket_name": "b"}, full=[change("aws_s3_bucket.cloudtrail")],
                   )
    r.n_destroys = 2
    res = run(aws_plan(s3, events), ACCT, ACCT, ctx=ctx(r, tmp_path))
    assert [x.step.kind for x in res] == [
        StepKind.COLLECTOR_STOP, StepKind.TARGETED_DESTROY, StepKind.EMPTY_BUCKET,
        StepKind.DESTROY, StepKind.VERIFY]
    assert events == ["stop"]
    assert r.calls[0] == ("plan", aws.POLICY_TARGETS)
    assert r.calls[1][0] == "apply" and r.calls[2] == ("plan", ())
    assert not s3.objs and not s3.marks
    assert max(s3.delete_sizes) == 1000 and sum(s3.delete_sizes) == 2800


def test_deny_policy_empty_fails_clearly():
    with pytest.raises(TeardownError, match="run the targeted policy destroy first"):
        aws.empty_bucket(FakeS3(versions=3, deny=True), "b")


def test_deny_via_client_exception():
    class Boom(Exception):
        response = {"Error": {"Code": "AccessDenied"}}

    class S3(FakeS3):
        def delete_objects(self, **kw):
            raise Boom()

    with pytest.raises(TeardownError, match="policy still denies deletes"):
        aws.empty_bucket(S3(versions=1), "b")


def test_wrong_confirm_refused(tmp_path):
    r = FakeRunner()
    with pytest.raises(ConfirmationError):
        run(aws_plan(FakeS3(), []), "nope", ACCT, ctx=ctx(r, tmp_path))
    assert r.calls == []


def test_dry_run_executes_nothing(tmp_path):
    events, r = [], FakeRunner()
    res = run(aws_plan(FakeS3(versions=1), events), "", ACCT, dry_run=True, ctx=ctx(r, tmp_path))
    assert len(res) == 5 and {x.status for x in res} == {"planned"}
    assert r.calls == [] and events == []


def test_unexpected_extra_target_refused(tmp_path):
    t = aws.POLICY_TARGETS
    r = FakeRunner(targeted={t: [change(a) for a in t] + [change("aws_sqs_queue.events")]})
    with pytest.raises(PlanReviewError, match="aws_sqs_queue.events"):
        run(aws_plan(FakeS3(), []), ACCT, ACCT, ctx=ctx(r, tmp_path))
    assert not any(c[0] == "apply" for c in r.calls)


def test_non_delete_action_refused(tmp_path):
    t = aws.POLICY_TARGETS
    r = FakeRunner(targeted={t: [change(t[0]), change(t[1], "update")]})
    with pytest.raises(PlanReviewError, match="non-delete"):
        run(aws_plan(FakeS3(), []), ACCT, ACCT, ctx=ctx(r, tmp_path))


def test_missing_expected_aws_target_refused(tmp_path):
    r = FakeRunner(targeted={aws.POLICY_TARGETS: [change("aws_cloudtrail.this")]})
    with pytest.raises(PlanReviewError, match="missing"):
        run(aws_plan(FakeS3(), []), ACCT, ACCT, ctx=ctx(r, tmp_path))


def test_verify_fails_if_state_not_empty(tmp_path):
    r = FakeRunner(outputs={"bucket_name": "b"}, left_after=["aws_iam_role.collector"])
    r.n_destroys = 2
    with pytest.raises(TeardownError, match="state not empty"):
        run(aws_plan(FakeS3(), []), ACCT, ACCT, ctx=ctx(r, tmp_path))


def test_azure_order(tmp_path):
    r = FakeRunner(targeted={azure.TARGETS: [
        change("azurerm_monitor_diagnostic_setting.activity"),
        change('azurerm_monitor_diagnostic_setting.aks_audit["a"]'),
        change("azurerm_eventhub_consumer_group.collector")]})
    r.n_destroys = 2
    events = []
    res = run(azure.build_plan(stop_collector=lambda: events.append("s")), ACCT, ACCT,
              ctx=ctx(r, tmp_path))
    assert [x.step.kind for x in res] == [
        StepKind.COLLECTOR_STOP, StepKind.TARGETED_DESTROY, StepKind.DESTROY, StepKind.VERIFY]
    assert r.calls[0] == ("plan", azure.TARGETS) and events == ["s"]


def test_gcp_order(tmp_path):
    r = FakeRunner(targeted={gcp.SINK_AND_SUBS: [
        change("google_logging_project_sink.logs[0]"),
        change("google_pubsub_subscription.collector")]})
    r.n_destroys = 3
    run(gcp.build_plan(stop_collector=None), ACCT, ACCT, ctx=ctx(r, tmp_path))
    plans = [c[1] for c in r.calls if c[0] == "plan"]
    assert plans[:3] == [gcp.SINK_AND_SUBS, gcp.TOPICS, ()]


def test_gcp_topic_step_rejects_extra(tmp_path):
    r = FakeRunner(targeted={gcp.TOPICS: [change("google_pubsub_topic.logs"),
                                          change("google_iam_workload_identity_pool.collector")]})
    with pytest.raises(PlanReviewError):
        run(gcp.build_plan(stop_collector=None), ACCT, ACCT, ctx=ctx(r, tmp_path))
