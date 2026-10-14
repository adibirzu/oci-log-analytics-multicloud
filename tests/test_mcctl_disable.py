import subprocess

import pytest
from typer.testing import CliRunner

import mcctl.cli as cli
import mcctl.plan as plan_mod
from mcctl.state import Layout, Manifest, write_manifest

runner = CliRunner()
ACCOUNT = "sub-0000-1111"


def change(addr):
    return {"address": addr, "change": {"actions": ["delete"]}}


class TeardownFake:
    """Fake terraform runner that logs every call; state empties after the full destroy."""

    def __init__(self, resources=("azurerm_eventhub.x",)):
        self.calls, self.resources, self._plans, self.full_applied = [], list(resources), {}, False

    def init(self):
        self.calls.append("init")

    def state_list(self):
        return [] if self.full_applied else list(self.resources)

    def state_serial(self):
        return 1

    def outputs(self):
        return {}

    def plan(self, out, destroy=False, targets=(), var_file=None):
        self.calls.append(("plan", tuple(targets)))
        self._plans[str(out)] = tuple(targets)

    def show_json(self, plan):
        t = self._plans[str(plan)]
        if t:
            return {"resource_changes": [change(a) for a in t]}
        if self.full_applied:
            return {"resource_changes": []}
        return {"resource_changes": [change(a) for a in self.resources]}

    def apply(self, plan):
        t = self._plans[str(plan)]
        self.calls.append(("apply", t))
        if not t:
            self.full_applied = True


def write_cfg(tmp_path, stop=True):
    p = tmp_path / "mcctl.yaml"
    extra = "    collector: {stop_command: [collector-stop, --now]}\n" if stop else ""
    p.write_text(f"""
clouds:
  azure:
    account: ${{TEST_AZ_ACCOUNT}}
    region: westeurope
    signals: [logs]
{extra}    oci: {{profile: cap, tenancy_name: tenancy-a, compartment_name: Staging}}
""")
    return str(p)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_AZ_ACCOUNT", ACCOUNT)
    st = {"fake": TeardownFake(), "cmds": [], "tty": False, "root": tmp_path / "repo"}
    deps = cli.Deps(lambda cc, sig, lay: st["fake"], lambda p, c: {"tenancy_name": "tenancy-a",
                                                                   "compartment_name": "Staging"},
                    tmp_path / "home", run_command=st["cmds"].append, is_tty=lambda: st["tty"],
                    repo_root=st["root"])
    monkeypatch.setattr(cli, "get_deps", lambda: deps)
    st["cfg"] = write_cfg(tmp_path)
    st["layout"] = Layout(tmp_path / "home", "azure", ACCOUNT, "logs")
    (st["root"] / "modules/logs/azure/terraform").mkdir(parents=True)
    return st


def run(env, *args):
    return runner.invoke(cli.app, ["disable", "azure", *args, "--config", env["cfg"]])


def seed_manifest(env):
    write_manifest(env["layout"], Manifest("p", "0" * 64, 1, {}))
    assert env["layout"].manifest_path.exists()


def test_dry_run_executes_nothing(env):
    r = run(env, "--dry-run")
    assert r.exit_code == 0, r.output
    assert "1. collector_stop" in r.output and "full destroy" in r.output
    assert not any(isinstance(c, tuple) for c in env["fake"].calls)
    assert env["cmds"] == []


@pytest.mark.parametrize("args", [(), ("--confirm-destroy", "wrong"), ("--yes",)])
def test_missing_or_wrong_confirm_refused(env, args):
    r = run(env, *args)
    assert r.exit_code == 2 and "REFUSED" in r.output
    assert env["fake"].calls == []


def test_non_tty_without_yes_refused(env):
    r = run(env, "--confirm-destroy", ACCOUNT)
    assert r.exit_code == 2 and "--yes" in r.output
    assert env["fake"].calls == []


def test_happy_path_in_order_and_manifest_removed(env):
    seed_manifest(env)
    r = run(env, "--confirm-destroy", ACCOUNT, "--yes")
    assert r.exit_code == 0, r.output
    from mcctl.teardown import azure as az
    plans = [c for c in env["fake"].calls if isinstance(c, tuple)]
    assert plans == [("plan", az.TARGETS), ("apply", az.TARGETS), ("plan", ()), ("apply", ()), ("plan", ())]
    assert env["cmds"] == [["collector-stop", "--now"]]
    assert "reviewed destroy plan" in r.output and "azurerm_eventhub.x" in r.output
    assert not env["layout"].manifest_path.exists()


def test_tty_prompt_accepts(env):
    env["tty"] = True
    r = runner.invoke(cli.app, ["disable", "azure", "--confirm-destroy", ACCOUNT, "--config", env["cfg"]],
                      input="y\n")
    assert r.exit_code == 0, r.output
    assert ("apply", ()) in env["fake"].calls


def test_no_stop_command_prints_reminder(env, tmp_path):
    env["cfg"] = write_cfg(tmp_path, stop=False)
    r = run(env, "--confirm-destroy", ACCOUNT, "--yes")
    assert r.exit_code == 0 and "reminder: stop the collector" in r.output and env["cmds"] == []


def test_absent_state_exit_zero(env):
    env["fake"] = TeardownFake(resources=())
    seed_manifest(env)
    r = run(env, "--confirm-destroy", ACCOUNT, "--yes")
    assert r.exit_code == 0 and "nothing to remove" in r.output
    assert not any(isinstance(c, tuple) for c in env["fake"].calls)


def test_protected_resource_refuses(env):
    (env["root"] / "modules/logs/azure/terraform/main.tf").write_text(
        'resource "azurerm_key_vault" "kv" {\n  lifecycle {\n    prevent_destroy = true\n  }\n}\n')
    r = run(env, "--confirm-destroy", ACCOUNT, "--yes")
    assert r.exit_code == 2 and "azurerm_key_vault.kv" in r.output and "REFUSED" in r.output
    assert env["fake"].calls == []
    assert run(env, "--dry-run").exit_code == 0  # report only


def test_stop_command_failure_is_error(env, monkeypatch):
    def boom(argv):
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(cli, "get_deps", lambda: cli.Deps(
        lambda cc, s, l: env["fake"], lambda p, c: {"tenancy_name": "tenancy-a", "compartment_name": "Staging"},
        env["layout"].home, run_command=boom, is_tty=lambda: False, repo_root=env["root"]))
    r = run(env, "--confirm-destroy", ACCOUNT, "--yes")
    assert r.exit_code == 1 and ("apply", ()) not in env["fake"].calls


def test_init_passes_path_backend_config_under_home(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(plan_mod.subprocess, "run", lambda argv, **kw: seen.append(argv) or
                        subprocess.CompletedProcess(argv, 0, "", ""))
    lay = Layout(tmp_path / "home", "aws", ACCOUNT, "logs")
    plan_mod.SubprocessRunner(tmp_path / "mod", lay.workdir, lay.state_path).init()
    args = seen[0]
    assert "-reconfigure" in args
    assert f"-backend-config=path={tmp_path / 'home' / 'state' / 'aws' / ACCOUNT / 'logs.tfstate'}" in args
    assert not any(a.startswith("-backend-config=key=") for a in args)
    assert (lay.state_path.parent.stat().st_mode & 0o777) == 0o700


def test_outputs_flattens_values(tmp_path, monkeypatch):
    monkeypatch.setattr(plan_mod.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, '{"bucket_name": {"value": "b", "type": "string"}}', ""))
    assert plan_mod.SubprocessRunner(tmp_path, tmp_path, tmp_path / "s").outputs() == {"bucket_name": "b"}
