"""macOS Notification Center delivery."""

from __future__ import annotations

import subprocess

from athlete_growth.application.automation.models import UserNotification

_DISPLAY_NOTIFICATION = """on run argv
    display notification (item 2 of argv) with title (item 1 of argv)
end run"""


class MacOSNotificationSink:
    """Send minimal notifications without interpolating user text into shell source."""

    def __init__(self, *, timeout_seconds: float = 5) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds

    def deliver(self, notification: UserNotification) -> None:
        subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                _DISPLAY_NOTIFICATION,
                "--",
                notification.title,
                notification.body,
            ],
            check=True,
            shell=False,
            timeout=self.timeout_seconds,
        )
