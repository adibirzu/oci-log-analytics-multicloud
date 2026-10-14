"""mcctl metrics/apm signals: enable targets modules/<signal>/<cloud>; disable uses the generic teardown."""
import dataclasses
from pathlib import Path

import pytest
from typer.testing import CliRunner

import mcctl.cli as cli
from fakes_mcctl import FakeRunner, load
from mcctl.config import REPO_ROOT
from mcctl.teardown import base as td_base, generic

runner = CliRunner()
ACCOUNT = "12" * 6  # fixture value, not a real account id


def write_cfg(path: Path, cloud: str, signals: str) -> str:
    p = path / "mcctl.yaml"
    p.write_text(f"""
clouds:
  {cloud}:
    account: ${{TEST_ACCOUNT}}
    region: eu-central-1
    signals: [{signals}]
    var_file: vars.tfvars
    collector: {{stop_command: [collector-stop, --now]}}
    oci: {{profile: cap, tenancy_name: tenancy-a, compartment_name: Staging}}
""")
    return str(p)


def oci_ok(profile, cfg):
    return {"tenancy_name": "tenancy-a", "compartment_name": "Staging"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_ACCOUNT", ACCOUNT)
    seen = []

    class RecordingRunner(FakeRunner):
        def __init__(self, module_dir, workdir, state_path):
            super().__init__(load("show_enable.json"), after_apply=load("show_noop.json"))
            seen.append((module_dir, state_path))

    monkeypatch.setattr(cli, "SubprocessRunner", RecordingRunner)
    deps = dataclasses.replace(cli.default_deps(), oci_identity=oci_ok, home=tmp_path / "home")
    monkeypatch.setattr(cli, "get_deps", lambda: deps)
    return {"seen": seen, "home": tmp_path / "home", "tmp": tmp_path}


def test_enable_aws_metrics_targets_metrics_module_and_state(env):
    cfg = write_cfg(env["tmp"], "aws", "logs, metrics")
    r = runner.invoke(cli.app, ["enable", "aws", "--signal", "metrics", "--plan-only", "--config", cfg])
    assert r.exit_code == 0, r.output
    assert "[aws/metrics] plan: +3 ~1" in r.output and "plan saved" in r.output
    (module_dir, state_path), = env["seen"]
    assert module_dir == REPO_ROOT / "modules" / "metrics" / "aws" / "terraform"
    assert module_dir.is_dir()
    assert str(state_path).endswith(f"/aws/{ACCOUNT}/metrics.tfstate")
    assert state_path.is_relative_to(env["home"] / "state")


def test_enable_rejects_signal_not_in_config(env):
    cfg = write_cfg(env["tmp"], "aws", "logs")
    r = runner.invoke(cli.app, ["enable", "aws", "--signal", "metrics", "--plan-only", "--config", cfg])
    assert r.exit_code == 1 and "not enabled in config" in r.output
    assert env["seen"] == []


def test_generic_plan_steps_and_delete_only_review(tmp_path):
    plan = generic.build_plan("azure", stop_collector=lambda: None)
    assert [s.kind for s in plan.steps] == [td_base.StepKind.COLLECTOR_STOP, td_base.StepKind.DESTROY,
                                            td_base.StepKind.VERIFY]
    no_stop = generic.build_plan("gcp")
    assert [s.kind for s in no_stop.steps] == [td_base.StepKind.DESTROY, td_base.StepKind.VERIFY]
    step = plan.steps[1]
    update = {"resource_changes": [{"address": "x.y", "change": {"actions": ["update"]}}]}
    with pytest.raises(td_base.PlanReviewError, match="non-delete"):
        td_base.review_destroy(step, update, plan.full_destroy_allowed)
    ok = {"resource_changes": [{"address": "x.y", "change": {"actions": ["delete"]}}]}
    assert td_base.review_destroy(step, ok, plan.full_destroy_allowed) == ("delete x.y",)


class MetricsFake:
    """Terraform fake: the full destroy deletes every resource, then state is empty."""

    def __init__(self):
        self.calls, self.done = [], False

    def init(self):
        self.calls.append("init")

    def state_list(self):
        return [] if self.done else ["azurerm_monitor_metric_alert.x"]

    def state_serial(self):
        return 1

    def outputs(self):
        return {}

    def plan(self, out, destroy=False, targets=(), var_file=None):
        self.calls.append(("plan", tuple(targets)))
        self._last = str(out)

    def show_json(self, plan):
        if self.done:
            return {"resource_changes": []}
        return {"resource_changes": [{"address": "azurerm_monitor_metric_alert.x",
                                      "change": {"actions": ["delete"]}}]}

    def apply(self, plan):
        self.calls.append("apply")
        self.done = True


def test_disable_metrics_runs_generic_teardown(tmp_path, monkeypatch):
    fake, cmds = MetricsFake(), []
    root = tmp_path / "repo"
    (root / "modules/metrics/azure/terraform").mkdir(parents=True)
    deps = cli.Deps(lambda cc, s, l: fake, oci_ok, tmp_path / "home", run_command=cmds.append,
                    is_tty=lambda: False, repo_root=root)
    monkeypatch.setattr(cli, "get_deps", lambda: deps)
    monkeypatch.setenv("TEST_ACCOUNT", ACCOUNT)
    cfg = write_cfg(tmp_path, "azure", "logs, metrics")
    args = ["disable", "azure", "--signal", "metrics", "--config", cfg]

    r = runner.invoke(cli.app, [*args, "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "1. collector_stop" in r.output and "2. destroy" in r.output and "3. verify" in r.output
    assert [c for c in fake.calls if c != "init"] == [] and cmds == []

    r = runner.invoke(cli.app, [*args, "--confirm-destroy", ACCOUNT, "--yes"])
    assert r.exit_code == 0, r.output
    assert cmds == [["collector-stop", "--now"]]
    # destroy plan -> apply of the reviewed plan -> verify plan (state empty)
    assert [c for c in fake.calls if c != "init"] == [("plan", ()), "apply", ("plan", ())]
    assert fake.done
    assert "removed" in r.output


def test_disable_apm_without_module_is_refused(tmp_path, monkeypatch):
    deps = cli.Deps(lambda cc, s, l: MetricsFake(), oci_ok, tmp_path / "home", repo_root=tmp_path / "repo")
    monkeypatch.setattr(cli, "get_deps", lambda: deps)
    monkeypatch.setenv("TEST_ACCOUNT", ACCOUNT)
    cfg = write_cfg(tmp_path, "gcp", "logs, apm")
    r = runner.invoke(cli.app, ["disable", "gcp", "--signal", "apm", "--dry-run", "--config", cfg])
    assert r.exit_code == 1 and "no terraform module for gcp/apm" in r.output
