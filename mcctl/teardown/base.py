"""Ordered, confirmed teardown engine shared by all clouds.

Safety rules: typed account confirmation, saved + reviewed destroy plans whose
resource set must match the step's expected targets, no ``force_destroy``,
and never any OCI-side resource.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcctl.plan import TerraformRunner  # single runner protocol shared with enable


class TeardownError(RuntimeError):
    """Teardown refused or failed; message is operator-facing."""


class ConfirmationError(TeardownError):
    pass


class PlanReviewError(TeardownError):
    pass


class StepKind(str, enum.Enum):
    COLLECTOR_STOP = "collector_stop"
    TARGETED_DESTROY = "targeted_destroy"
    EMPTY_BUCKET = "empty_bucket"
    DESTROY = "destroy"
    VERIFY = "verify"


@dataclass(frozen=True)
class TeardownStep:
    kind: StepKind
    description: str
    targets: tuple[str, ...] = ()
    strict: bool = False  # every target must appear in the plan
    action: Callable[["TeardownContext"], None] | None = None


@dataclass
class TeardownContext:
    runner: TerraformRunner
    workdir: Path
    var_file: str | None = None
    outputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TeardownPlan:
    cloud: str
    steps: tuple[TeardownStep, ...]
    # Resources a full destroy is allowed to remove; None = not constrained.
    full_destroy_allowed: frozenset[str] | None = None


@dataclass(frozen=True)
class StepResult:
    step: TeardownStep
    status: str  # planned | done
    summary: tuple[str, ...] = ()


def _base(address: str) -> str:
    return address.split("[", 1)[0]


def summarize(plan_json: Mapping[str, Any]) -> list[tuple[str, tuple[str, ...]]]:
    return [
        (rc["address"], tuple(rc.get("change", {}).get("actions", ())))
        for rc in plan_json.get("resource_changes", ())
    ]


def review_destroy(step: TeardownStep, plan_json: Mapping[str, Any],
                   allowed: frozenset[str] | None) -> tuple[str, ...]:
    changes = summarize(plan_json)
    lines = tuple(f"{'+'.join(a) or 'no-op'} {addr}" for addr, a in changes)
    bad = [addr for addr, a in changes if tuple(a) not in (("delete",), ("no-op",))]
    if bad:
        raise PlanReviewError(f"{step.description}: non-delete actions planned for {sorted(bad)}")
    deleted = {addr for addr, a in changes if tuple(a) == ("delete",)}
    expected = frozenset(step.targets) if step.kind is StepKind.TARGETED_DESTROY else allowed
    if expected is not None:
        extra = sorted(a for a in deleted if _base(a) not in expected)
        if extra:
            raise PlanReviewError(f"{step.description}: unexpected destroy targets {extra}")
    if step.strict:
        missing = sorted(t for t in step.targets if not any(_base(a) == t for a in deleted))
        if missing:
            raise PlanReviewError(f"{step.description}: expected targets missing from plan {missing}")
    return lines


def _outputs(plan_json: Mapping[str, Any]) -> dict[str, Any]:
    outs = plan_json.get("prior_state", {}).get("values", {}).get("outputs", {})
    return {k: v.get("value") for k, v in outs.items()}


def _run_destroy(step: TeardownStep, idx: int, plan: TeardownPlan, ctx: TeardownContext,
                 on_review: Callable[[TeardownStep, tuple[str, ...]], None] | None) -> tuple[str, ...]:
    out = str(ctx.workdir / f"teardown-{idx}.tfplan")
    ctx.runner.plan(out, True, list(step.targets), ctx.var_file)
    pj = ctx.runner.show_json(out)
    ctx.outputs.update({k: v for k, v in _outputs(pj).items() if v is not None})
    lines = review_destroy(step, pj, plan.full_destroy_allowed)
    if on_review is not None:
        on_review(step, lines)
    ctx.runner.apply(out)
    return lines


def _run_verify(idx: int, ctx: TeardownContext) -> tuple[str, ...]:
    out = str(ctx.workdir / f"teardown-{idx}.tfplan")
    ctx.runner.plan(out, True, [], ctx.var_file)
    pj = ctx.runner.show_json(out)
    left = [a for a, _ in summarize(pj)]
    root = pj.get("prior_state", {}).get("values", {}).get("root_module", {})
    left += [r["address"] for r in root.get("resources", ())]
    if left:
        raise TeardownError(f"state not empty after destroy: {sorted(set(left))}")
    return ("state empty",)


def run(plan: TeardownPlan, confirm: str, expected_account: str, *,
        ctx: TeardownContext | None = None, dry_run: bool = False,
        on_review: Callable[[TeardownStep, tuple[str, ...]], None] | None = None) -> list[StepResult]:
    if dry_run:
        return [StepResult(s, "planned") for s in plan.steps]
    if not expected_account or confirm != expected_account:
        raise ConfirmationError("typed confirmation does not match the account; refusing teardown")
    if ctx is None:
        raise TeardownError("a TeardownContext is required to execute")
    ctx.outputs.update({k: v for k, v in ctx.runner.outputs().items() if v is not None})
    results: list[StepResult] = []
    for idx, step in enumerate(plan.steps):
        if step.kind in (StepKind.TARGETED_DESTROY, StepKind.DESTROY):
            summary = _run_destroy(step, idx, plan, ctx, on_review)
        elif step.kind is StepKind.VERIFY:
            summary = _run_verify(idx, ctx)
        else:
            if step.action is not None:
                step.action(ctx)
            summary = ()
        results.append(StepResult(step, "done", summary))
    return results
