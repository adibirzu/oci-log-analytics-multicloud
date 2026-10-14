"""Terraform runner protocol, plan summary, sha256 and stale-plan guard."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol, Sequence

from mcctl.state import Layout, Manifest, read_manifest


class StalePlanError(RuntimeError):
    pass


class TerraformError(RuntimeError):
    pass


class TerraformRunner(Protocol):
    def init(self) -> None: ...
    def plan(self, out: Path, destroy: bool = False, targets: Sequence[str] = (),
             var_file: str | None = None) -> None: ...
    def show_json(self, plan: Path) -> dict: ...
    def apply(self, plan: Path) -> None: ...
    def state_serial(self) -> int: ...
    def state_list(self) -> list[str]: ...
    def outputs(self) -> dict: ...


class SubprocessRunner:
    """Runs terraform. Secrets never go on argv: only a var-file path or TF_VAR_* env."""

    def __init__(self, module_dir: Path, workdir: Path, state_path: Path, binary: str = "terraform"):
        self.module_dir, self.workdir, self.state_path, self.binary = module_dir, workdir, state_path, binary

    def _run(self, args: list[str]) -> str:
        env = dict(os.environ, TF_IN_AUTOMATION="1", TF_INPUT="0",
                   TF_DATA_DIR=str(self.workdir / ".terraform"))
        r = subprocess.run([self.binary, f"-chdir={self.module_dir}", *args], env=env,
                           capture_output=True, text=True, check=False)
        if r.returncode != 0:
            raise TerraformError(f"terraform {args[0]} failed (exit {r.returncode}): {(r.stderr or '')[-2000:]}")
        return r.stdout or ""

    def init(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_path.parent, 0o700)
        self._run(["init", "-input=false", "-no-color", "-reconfigure",
                   f"-backend-config=path={self.state_path}"])

    def plan(self, out, destroy=False, targets=(), var_file=None) -> None:
        args = ["plan", "-input=false", "-no-color", f"-out={out}"]
        if destroy:
            args.append("-destroy")
        if var_file:
            args.append(f"-var-file={var_file}")
        args += [f"-target={t}" for t in targets]
        self._run(args)

    def show_json(self, plan) -> dict:
        return json.loads(self._run(["show", "-json", str(plan)]))

    def apply(self, plan) -> None:
        self._run(["apply", "-input=false", "-no-color", str(plan)])

    def state_serial(self) -> int:
        try:
            return int(json.loads(self._run(["state", "pull"]) or "{}").get("serial", 0))
        except TerraformError:
            return 0

    def outputs(self) -> dict:
        try:
            raw = json.loads(self._run(["output", "-json"]) or "{}")
        except TerraformError:
            return {}
        return {k: v.get("value") for k, v in raw.items() if isinstance(v, dict)}

    def state_list(self) -> list[str]:
        try:
            return [x for x in self._run(["state", "list"]).splitlines() if x]
        except TerraformError:
            return []


_IAM = re.compile(r"(^aws_iam_|iam|role_assignment|role_definition|^oci_identity_|_policy$|service_account|"
                  r"federated_identity|workload_identity)")


@dataclass(frozen=True)
class PlanSummary:
    add: int = 0
    change: int = 0
    destroy: int = 0
    replace: int = 0
    iam_changes: list[str] = field(default_factory=list)
    destroys: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.add or self.change or self.destroy or self.replace)

    def to_dict(self) -> dict:
        return asdict(self)


def summarize(show: dict) -> PlanSummary:
    add = change = destroy = replace = 0
    iam: list[str] = []
    destroys: list[str] = []
    for rc in show.get("resource_changes") or []:
        actions = list((rc.get("change") or {}).get("actions") or [])
        if actions in (["no-op"], ["read"], []):
            continue
        addr, typ = rc.get("address", "?"), rc.get("type", "")
        if "delete" in actions and "create" in actions:
            replace += 1
            destroys.append(addr)
        elif actions == ["create"]:
            add += 1
        elif actions == ["update"]:
            change += 1
        elif actions == ["delete"]:
            destroy += 1
            destroys.append(addr)
        if _IAM.search(typ):
            iam.append(f"{','.join(actions)} {addr}")
    return PlanSummary(add, change, destroy, replace, iam, destroys)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def apply_saved(runner: TerraformRunner, layout: Layout) -> None:
    m: Manifest | None = read_manifest(layout)
    if m is None:
        raise StalePlanError("no manifest for this plan; run plan first")
    plan = Path(m.plan_path)
    if not plan.is_file():
        raise StalePlanError(f"saved plan missing: {plan}")
    if sha256_file(plan) != m.plan_sha256:
        raise StalePlanError("plan file sha256 differs from manifest; refusing to apply")
    if runner.state_serial() != m.state_serial:
        raise StalePlanError("state serial changed since plan was created (stale plan); re-plan")
    runner.apply(plan)
