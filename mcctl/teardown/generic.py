"""Generic teardown for signals without a cloud-specific ordering (metrics, apm).

Steps: optional collector-stop reminder/action, one full destroy whose plan
must contain only ``delete`` actions (enforced by ``base.review_destroy``),
then a verify that state is empty. No bucket or policy ordering is needed.
"""

from __future__ import annotations

from collections.abc import Callable

from mcctl.teardown.base import StepKind, TeardownContext, TeardownPlan, TeardownStep

SUPPORTED_CLOUDS = ("aws", "azure", "gcp")


def build_plan(cloud: str, *, stop_collector: Callable[[], None] | None = None) -> TeardownPlan:
    if cloud not in SUPPORTED_CLOUDS:
        raise ValueError(f"unknown cloud {cloud!r}")
    steps: list[TeardownStep] = []
    if stop_collector is not None:
        def _stop(_ctx: TeardownContext) -> None:
            stop_collector()

        steps.append(TeardownStep(StepKind.COLLECTOR_STOP, "stop the collector", action=_stop))
    steps.append(TeardownStep(StepKind.DESTROY, "full destroy (reviewed: delete actions only)"))
    steps.append(TeardownStep(StepKind.VERIFY, "verify state is empty"))
    return TeardownPlan(cloud, tuple(steps))
