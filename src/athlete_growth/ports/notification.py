"""Notification delivery boundary."""

from __future__ import annotations

from typing import Protocol

from athlete_growth.application.automation.models import UserNotification


class NotificationSink(Protocol):
    """Deliver a previously persisted user notification."""

    def deliver(self, notification: UserNotification) -> None: ...


class NoopNotificationSink:
    """Portable sink for hosts without proactive notification support."""

    def deliver(self, notification: UserNotification) -> None:
        del notification
