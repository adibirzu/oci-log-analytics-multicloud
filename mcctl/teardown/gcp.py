"""GCP teardown: collector stop, sink+subscriptions, topics, full destroy."""

from __future__ import annotations

from collections.abc import Callable

from mcctl.teardown.base import StepKind, TeardownContext, TeardownPlan, TeardownStep

SINK_AND_SUBS = (
    "google_logging_project_sink.logs",
    "google_logging_organization_sink.logs",
    "google_pubsub_subscription.collector",
    "google_pubsub_subscription.dead_letter",
)
TOPICS = ("google_pubsub_topic.logs", "google_pubsub_topic.dead_letter")


def build_plan(*, stop_collector: Callable[[], None] | None) -> TeardownPlan:
    def _stop(_ctx: TeardownContext) -> None:
        if stop_collector is not None:
            stop_collector()

    return TeardownPlan("gcp", (
        TeardownStep(StepKind.COLLECTOR_STOP, "stop the collector", action=_stop),
        TeardownStep(StepKind.TARGETED_DESTROY, "destroy log sink and subscriptions",
                     targets=SINK_AND_SUBS),
        TeardownStep(StepKind.TARGETED_DESTROY, "destroy topics", targets=TOPICS),
        TeardownStep(StepKind.DESTROY, "full destroy"),
        TeardownStep(StepKind.VERIFY, "verify state is empty"),
    ))
