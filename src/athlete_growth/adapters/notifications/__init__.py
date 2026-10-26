"""System notification adapters."""

from __future__ import annotations

import sys

from athlete_growth.adapters.notifications.macos import MacOSNotificationSink
from athlete_growth.ports.notification import NoopNotificationSink, NotificationSink


def default_notification_sink() -> NotificationSink:
    """Return a native sink when supported, otherwise a portable no-op."""

    if sys.platform == "darwin":
        return MacOSNotificationSink()
    return NoopNotificationSink()


__all__ = ["MacOSNotificationSink", "NoopNotificationSink", "default_notification_sink"]
