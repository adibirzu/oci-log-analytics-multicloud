import pytest
from typer.testing import CliRunner

import mcctl.cli as cli
from fakes_mcctl import FakeRunner, load
from mcctl.state import Layout, read_manifest

runner = CliRunner()
ACCOUNT = "111122223333"


def write_cfg(tmp_path, profile="cap", comp="Staging", protected=""):
    p = tmp_path / "mcctl.yaml"
    p.write_text(f"""{protected}
clouds:
  aws:
    account: ${{TEST_AWS_ACCOUNT}}
    region: eu-central-1
    signals: [logs]
    var_file: vars.tfvars
    oci: {{profile: {profile}, tenancy_name: tenancy-a, compartment_name: {comp}}}
""")
    return str(p)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_AWS_ACCOUNT", ACCOUNT)
    state = {"runner": FakeRunner(load("show_enable.json"), after_apply=load("show_noop.json")),
             "ident": {"tenancy_name": "tenancy-a", "compartment_name": "Staging"}}
    deps = cli.Deps(lambda cc, sig, lay: state["runner"], lambda p, c: state["ident"], tmp_path / "home")
    monkeypatch.setattr(cli, "get_deps", lambda: deps)
    state["cfg"] = write_cfg(tmp_path)
    state["home"] = tmp_path / "home"
    return state


def run(env, *args):
    return runner.invoke(cli.app, [*args, "--config", env["cfg"]])


def test_plan_only_stops_before_apply(env):
    r = run(env, "enable", "aws", "--plan-only", "--yes")
    assert r.exit_code == 0, r.output
    assert "+3 ~1" in r.output and "IAM/policy: create aws_iam_role.ingest" in r.output
    assert "apply" not in env["runner"].calls
    m = read_manifest(Layout(env["home"], "aws", ACCOUNT, "logs"))
    assert m and len(m.plan_sha256) == 64


def test_without_yes_stops_for_review(env):
    r = run(env, "enable", "aws")
    assert r.exit_code == 0 and "re-run with --apply" in r.output
    assert "apply" not in env["runner"].calls


def test_idempotent_reenable(env):
    r1 = run(env, "enable", "aws", "--yes")
    assert r1.exit_code == 0, r1.output
    assert env["runner"].calls.count("apply") == 1 and "aws/logs: enabled" in r1.output
    r2 = run(env, "enable", "aws", "--yes")
    assert r2.exit_code == 0 and "no changes" in r2.output
    assert env["runner"].calls.count("apply") == 1


def test_refuses_removal_in_enable(env):
    env["runner"].show = load("show_replace.json")
    r = run(env, "enable", "aws", "--yes")
    assert r.exit_code == 1 and "REFUSED" in r.output
    assert "apply" not in env["runner"].calls
    assert read_manifest(Layout(env["home"], "aws", ACCOUNT, "logs")) is None


def test_stale_plan_refused(env, monkeypatch):
    fr = env["runner"]
    seq = iter([1, 2])  # manifest records 1, apply-time check sees 2
    monkeypatch.setattr(fr, "state_serial", lambda: next(seq, 2))
    r = run(env, "enable", "aws", "--yes")
    assert r.exit_code == 1 and "stale" in r.output
    assert "apply" not in fr.calls


PROTECTED = "protected_profiles: {prod-profile: AllowedCompartment}"


def test_protected_profile_guard(env, tmp_path):
    env["cfg"] = write_cfg(tmp_path, profile="prod-profile", comp="Other", protected=PROTECTED)
    r = run(env, "enable", "aws", "--yes")
    assert r.exit_code == 1 and "read-only" in r.output
    assert env["runner"].calls == []


def test_protected_profile_ok_in_allowed_compartment(env, tmp_path):
    env["cfg"] = write_cfg(tmp_path, profile="prod-profile", comp="AllowedCompartment", protected=PROTECTED)
    env["ident"] = {"tenancy_name": "tenancy-a", "compartment_name": "AllowedCompartment"}
    assert run(env, "enable", "aws", "--plan-only").exit_code == 0


def test_no_protected_profiles_by_default(env, tmp_path):
    env["cfg"] = write_cfg(tmp_path, profile="prod-profile", comp="Other")
    env["ident"] = {"tenancy_name": "tenancy-a", "compartment_name": "Other"}
    assert run(env, "enable", "aws", "--plan-only").exit_code == 0


def test_tenancy_mismatch_refused(env):
    env["ident"] = {"tenancy_name": "someone-else", "compartment_name": "Staging"}
    r = run(env, "enable", "aws", "--yes")
    assert r.exit_code == 1 and "tenancy" in r.output and env["runner"].calls == []


def test_status_absent_then_enabled(env):
    assert "aws/logs: absent" in run(env, "status", "aws").output
    run(env, "enable", "aws", "--yes")
    assert "aws/logs: enabled" in run(env, "status", "aws", "--signal", "logs").output


def test_unset_env_var_is_error(env, monkeypatch):
    monkeypatch.delenv("TEST_AWS_ACCOUNT")
    r = run(env, "enable", "aws")
    assert r.exit_code == 1 and "TEST_AWS_ACCOUNT" in r.output


def test_enable_apply_uses_saved_plan_without_replanning(tmp_path, monkeypatch):
    import mcctl.cli as cli

    calls = []
    monkeypatch.setattr(cli, "apply_saved", lambda runner, layout: calls.append("apply_saved"))

    class Runner:
        def init(self):
            calls.append("init")

        def plan(self, *a, **k):
            calls.append("plan")

    class CC:
        cloud, account = "aws", "acct"

    deps = type("D", (), {"home": tmp_path, "runner_factory": staticmethod(lambda cc, s, l: Runner())})()
    cli._enable_one(CC(), "logs", deps, plan_only=False, yes=False, apply_reviewed=True)
    assert calls == ["init", "apply_saved"]
