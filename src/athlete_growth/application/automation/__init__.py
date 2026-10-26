"""Application records for durable sync, analysis, and notifications."""

from typing import TYPE_CHECKING

from athlete_growth.application.automation.models import (
    AnalysisSnapshot,
    DataChangeSet,
    NotificationMode,
    SyncRun,
    SyncSubscription,
    UserNotification,
)

if TYPE_CHECKING:
    from athlete_growth.application.automation.service import (
        AutomaticTrainingLoop,
        AutomationSummary,
        DeliverySummary,
    )


def __getattr__(name: str):
    if name in {"AutomaticTrainingLoop", "AutomationSummary", "DeliverySummary"}:
        from athlete_growth.application.automation import service

        return getattr(service, name)
    raise AttributeError(name)


__all__ = [
    "AnalysisSnapshot",
    "AutomaticTrainingLoop",
    "AutomationSummary",
    "DataChangeSet",
    "DeliverySummary",
    "NotificationMode",
    "SyncRun",
    "SyncSubscription",
    "UserNotification",
]
