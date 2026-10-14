"""Azure teardown: collector stop, diagnostics + consumer group, full destroy."""

from __future__ import annotations

from collections.abc import Callable

from mcctl.teardown.base import StepKind, TeardownContext, TeardownPlan, TeardownStep

TARGETS = (
    "azurerm_monitor_diagnostic_setting.activity",
    "azurerm_monitor_aad_diagnostic_setting.entra",
    "azurerm_monitor_diagnostic_setting.aks_audit",
    "azurerm_eventhub_consumer_group.collector",
)


def build_plan(*, stop_collector: Callable[[], None] | None) -> TeardownPlan:
    def _stop(_ctx: TeardownContext) -> None:
        if stop_collector is not None:
            stop_collector()

    return TeardownPlan("azure", (
        TeardownStep(StepKind.COLLECTOR_STOP, "stop the collector", action=_stop),
        TeardownStep(StepKind.TARGETED_DESTROY,
                     "destroy diagnostic settings and consumer group", targets=TARGETS),
        TeardownStep(StepKind.DESTROY, "full destroy"),
        TeardownStep(StepKind.VERIFY, "verify state is empty"),
    ))
