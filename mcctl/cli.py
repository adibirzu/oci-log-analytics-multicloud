"""mcctl command line: enable / disable / status."""
from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import typer

from mcctl import preflight
from mcctl.config import REPO_ROOT, SIGNALS, CloudConfig, ConfigError, load_config
from mcctl.plan import (StalePlanError, SubprocessRunner, TerraformError, TerraformRunner, apply_saved,
                        sha256_file, summarize)
from mcctl.teardown import aws as td_aws, azure as td_azure, base as td_base, gcp as td_gcp, generic as td_generic
from mcctl.state import Layout, LockBusy, Manifest, file_lock, mcctl_home, write_manifest

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Enable/disable multicloud -> OCI telemetry.")


TeardownFactory = Callable[[CloudConfig, str, Callable[[], None]], td_base.TeardownPlan]


def build_teardown_plan(cc: CloudConfig, signal: str, stop_collector: Callable[[], None]) -> td_base.TeardownPlan:
    if signal != "logs":
        return td_generic.build_plan(cc.cloud, stop_collector=stop_collector)
    if cc.cloud == "aws":
        return td_aws.build_plan(stop_collector=stop_collector, s3=td_aws.make_s3_client(cc.region))
    if cc.cloud == "azure":
        return td_azure.build_plan(stop_collector=stop_collector)
    return td_gcp.build_plan(stop_collector=stop_collector)


def _run_command(argv: list[str]) -> None:
    subprocess.run(argv, check=True, shell=False)


@dataclass
class Deps:
    runner_factory: Callable[[CloudConfig, str, Layout], TerraformRunner]
    oci_identity: preflight.OciIdentity
    home: Path
    teardown_factory: TeardownFactory = build_teardown_plan
    run_command: Callable[[list[str]], None] = _run_command
    is_tty: Callable[[], bool] = field(default=lambda: sys.stdin.isatty())
    repo_root: Path = REPO_ROOT


def default_deps() -> Deps:
    home = mcctl_home()

    def factory(cc: CloudConfig, signal: str, layout: Layout) -> TerraformRunner:
        return SubprocessRunner(cc.module_dir(signal, REPO_ROOT), layout.workdir, layout.state_path)

    return Deps(factory, preflight.sdk_identity, home)


def get_deps() -> Deps:  # monkeypatched in tests
    return default_deps()


def _signals(cc: CloudConfig, signal: Optional[str]) -> list[str]:
    if signal is None:
        return list(cc.signals)
    if signal not in SIGNALS:
        raise ConfigError(f"unknown signal {signal!r}")
    if signal not in cc.signals:
        raise ConfigError(f"signal {signal!r} is not enabled in config for {cc.cloud}")
    return [signal]


def _enable_one(cc: CloudConfig, signal: str, deps: Deps, plan_only: bool, yes: bool,
                apply_reviewed: bool = False) -> None:
    layout = Layout(deps.home, cc.cloud, cc.account, signal)
    layout.ensure()
    with file_lock(layout.lock_path):
        runner = deps.runner_factory(cc, signal, layout)
        runner.init()
        if apply_reviewed:
            # Apply exactly the plan reviewed earlier: apply_saved checks its sha256 and state serial.
            apply_saved(runner, layout)
            typer.echo(f"[{cc.cloud}/{signal}] applied reviewed plan")
            return
        runner.plan(layout.plan_path, destroy=False, targets=(), var_file=cc.var_file)
        summary = summarize(runner.show_json(layout.plan_path))
        typer.echo(f"[{cc.cloud}/{signal}] plan: +{summary.add} ~{summary.change} -{summary.destroy} "
                   f"replace {summary.replace}")
        for line in summary.iam_changes:
            typer.echo(f"  IAM/policy: {line}")
        if summary.destroy or summary.replace:
            typer.echo(f"  REFUSED: enable plan would remove/replace: {', '.join(summary.destroys)}", err=True)
            raise typer.Exit(1)
        if summary.is_empty:
            typer.echo(f"[{cc.cloud}/{signal}] no changes")
            return
        write_manifest(layout, Manifest(str(layout.plan_path), sha256_file(layout.plan_path),
                                        runner.state_serial(), summary.to_dict()))
        if plan_only or not yes:
            typer.echo(f"  plan saved ({layout.plan_path}); review, then re-run with --apply")
            return
        apply_saved(runner, layout)
        typer.echo(f"[{cc.cloud}/{signal}] applied")


def _status(cc: CloudConfig, signals: list[str], deps: Deps) -> None:
    for s in signals:
        layout = Layout(deps.home, cc.cloud, cc.account, s)
        n = len(deps.runner_factory(cc, s, layout).state_list())
        typer.echo(f"{cc.cloud}/{s}: {'enabled (' + str(n) + ' resources)' if n else 'absent'}")


@app.command()
def enable(cloud: str, signal: Optional[str] = typer.Option(None, "--signal"),
           plan_only: bool = typer.Option(False, "--plan-only"), yes: bool = typer.Option(False, "--yes"),
           apply_reviewed: bool = typer.Option(False, "--apply", help="apply the saved, reviewed plan as-is"),
           config: Optional[str] = typer.Option(None, "--config", envvar="MCCTL_CONFIG")) -> None:
    """Plan the signals for a cloud; --apply applies the reviewed plan, --yes plans and applies in one run."""
    deps = get_deps()
    try:
        cc = load_config(config).cloud(cloud)
        signals = _signals(cc, signal)
        preflight.check(cc, deps.oci_identity)
        for s in signals:
            _enable_one(cc, s, deps, plan_only, yes, apply_reviewed)
    except (ConfigError, preflight.PreflightError, StalePlanError, TerraformError, LockBusy) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    if (yes or apply_reviewed) and not plan_only:
        _status(cc, signals, deps)


@app.command()
def status(cloud: str, signal: Optional[str] = typer.Option(None, "--signal"),
           config: Optional[str] = typer.Option(None, "--config", envvar="MCCTL_CONFIG")) -> None:
    """Show enabled/absent per signal from Terraform state (no cloud calls)."""
    deps = get_deps()
    try:
        cc = load_config(config).cloud(cloud)
        _status(cc, _signals(cc, signal), deps)
    except (ConfigError, TerraformError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)


_RESOURCE = re.compile(r'^\s*resource\s+"([^"]+)"\s+"([^"]+)"')
_PROTECT = re.compile(r"^\s*prevent_destroy\s*=\s*true\b")


def protected_resources(module_dir: Path) -> list[str]:
    """Static scan of the module's .tf files for resources carrying prevent_destroy = true."""
    found: list[str] = []
    for tf in sorted(module_dir.glob("*.tf")):
        current = None
        for line in tf.read_text().splitlines():
            m = _RESOURCE.match(line)
            if m:
                current = f"{m.group(1)}.{m.group(2)}"
            elif current and _PROTECT.match(line.split("#", 1)[0]):
                found.append(current)
    return found


def _stop_collector(cc: CloudConfig, deps: Deps) -> Callable[[], None]:
    def stop() -> None:
        if not cc.stop_command:
            typer.echo("  reminder: stop the collector yourself (no collector.stop_command configured)")
            return
        deps.run_command(list(cc.stop_command))
        typer.echo("  collector stopped")

    return stop


def _disable_one(cc: CloudConfig, signal: str, deps: Deps, dry_run: bool, yes: bool,
                 confirm: Optional[str]) -> None:
    layout = Layout(deps.home, cc.cloud, cc.account, signal)
    layout.ensure()
    with file_lock(layout.lock_path):
        runner = deps.runner_factory(cc, signal, layout)
        runner.init()
        if not runner.state_list():
            typer.echo(f"[{cc.cloud}/{signal}] nothing to remove")
            layout.manifest_path.unlink(missing_ok=True)
            return
        plan = deps.teardown_factory(cc, signal, _stop_collector(cc, deps))
        typer.echo(f"[{cc.cloud}/{signal}] teardown steps:")
        for i, step in enumerate(plan.steps, 1):
            typer.echo(f"  {i}. {step.kind.value}: {step.description}")
        if dry_run:
            typer.echo("  dry-run: nothing executed")
            return
        ctx = td_base.TeardownContext(runner, layout.workdir, cc.var_file)

        def on_review(step: td_base.TeardownStep, lines: tuple[str, ...]) -> None:
            typer.echo(f"  reviewed destroy plan ({step.description}):")
            for line in lines:
                typer.echo(f"    {line}")

        td_base.run(plan, confirm or "", cc.account, ctx=ctx, on_review=on_review)
        layout.manifest_path.unlink(missing_ok=True)
        typer.echo(f"[{cc.cloud}/{signal}] removed")


@app.command(name="disable")
def disable_cmd(cloud: str, signal: str = typer.Option("logs", "--signal"),
                dry_run: bool = typer.Option(False, "--dry-run"),
                confirm_destroy: Optional[str] = typer.Option(None, "--confirm-destroy"),
                yes: bool = typer.Option(False, "--yes"),
                config: Optional[str] = typer.Option(None, "--config", envvar="MCCTL_CONFIG")) -> None:
    """Ordered, confirmed teardown of a cloud's signal (state-tracked resources only)."""
    deps = get_deps()
    try:
        cc = load_config(config).cloud(cloud)
        _signals(cc, signal)
        if signal != "logs" and not cc.module_dir(signal, deps.repo_root).is_dir():
            raise ConfigError(f"no terraform module for {cc.cloud}/{signal}; disable supports logs and modules "
                              f"that exist under modules/{signal}/{cc.cloud}/terraform")
        preflight.check(cc, deps.oci_identity)
        protected = protected_resources(cc.module_dir(signal, deps.repo_root))
        if protected:
            typer.echo("protected resources (prevent_destroy):", err=True)
            for r in protected:
                typer.echo(f"  {r}", err=True)
            if not dry_run:
                typer.echo("REFUSED: remove them from config scope first", err=True)
                raise typer.Exit(2)
        if not dry_run:
            if confirm_destroy != cc.account:
                typer.echo(f"REFUSED: --confirm-destroy must equal the configured account ({cc.cloud})", err=True)
                raise typer.Exit(2)
            if not yes:
                if not deps.is_tty():
                    typer.echo("REFUSED: non-interactive run requires --yes with --confirm-destroy", err=True)
                    raise typer.Exit(2)
                typer.confirm(f"Destroy {cc.cloud}/{signal} for account {cc.account}?", abort=True)
        _disable_one(cc, signal, deps, dry_run, yes, confirm_destroy)
    except (ConfigError, preflight.PreflightError, TerraformError, LockBusy, td_base.TeardownError,
            subprocess.CalledProcessError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
