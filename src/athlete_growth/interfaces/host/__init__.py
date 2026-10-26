"""Host-neutral automation facade."""

from athlete_growth.interfaces.host.tools import (
    automation_configure,
    automation_install_macos,
    automation_run_once,
    automation_status,
    automation_uninstall_macos,
    configure_automation,
    install_macos_automation,
    list_notifications,
    notification_list,
    notification_read,
    read_notification,
    run_automation_once,
    uninstall_macos_automation,
)

__all__ = [
    "automation_configure",
    "automation_install_macos",
    "automation_run_once",
    "automation_status",
    "automation_uninstall_macos",
    "configure_automation",
    "install_macos_automation",
    "list_notifications",
    "notification_list",
    "notification_read",
    "read_notification",
    "run_automation_once",
    "uninstall_macos_automation",
]
