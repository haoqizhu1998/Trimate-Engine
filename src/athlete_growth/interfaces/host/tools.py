"""JSON-safe functions for CLI and future model-host adapters."""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from athlete_growth.adapters.garmin_cn import GarminCnConnector
from athlete_growth.adapters.notifications import default_notification_sink
from athlete_growth.adapters.scheduler import MacOSLaunchdScheduler
from athlete_growth.adapters.storage import (
    ProfileRegistry,
    SQLiteAthleteProfileRepository,
    SQLiteAutomationRepository,
    SQLiteRecordStore,
    VaultLayout,
)
from athlete_growth.application.automation import (
    AutomaticTrainingLoop,
    NotificationMode,
    SyncRun,
    SyncSubscription,
    UserNotification,
)

DEFAULT_SOURCE = "garmin-cn"
DEFAULT_INTERVAL_MINUTES = 30
DEFAULT_LOOKBACK_DAYS = 3
DEFAULT_NOTIFICATION_MODE = NotificationMode.ACTIONABLE_DIGEST
DEFAULT_DIGEST_HOUR_LOCAL = 20
_SANITIZED_SYNC_ERROR = re.compile(
    r"(?P<error_class>[A-Za-z_][A-Za-z0-9_]{0,63}): automatic sync failed"
)
_SAFE_ERROR_CLASSES = frozenset(
    {
        "ConnectionError",
        "FileNotFoundError",
        "OSError",
        "PermissionError",
        "RuntimeError",
        "TimeoutError",
        "ValueError",
    }
)


@dataclass(frozen=True, slots=True)
class _HostRuntime:
    home: Path
    athlete_id: str
    layout: VaultLayout
    profiles: SQLiteAthleteProfileRepository
    records: SQLiteRecordStore
    automation: SQLiteAutomationRepository

    @classmethod
    def load(cls, data_home: str | Path) -> _HostRuntime:
        home = Path(data_home).expanduser().resolve()
        athlete_id = ProfileRegistry(home / "config.toml").default_profile_id()
        if athlete_id is None:
            raise RuntimeError("data home is not initialized")
        layout = VaultLayout(home, athlete_id)
        if not layout.database_file.is_file():
            raise RuntimeError("default profile database is missing")
        profiles = SQLiteAthleteProfileRepository(layout.database_file)
        if profiles.get_by_id(athlete_id) is None:
            raise RuntimeError("default profile record is missing")
        return cls(
            home=home,
            athlete_id=athlete_id,
            layout=layout,
            profiles=profiles,
            records=SQLiteRecordStore(layout.database_file),
            automation=SQLiteAutomationRepository(layout.database_file),
        )


def configure_automation(
    data_home: str | Path,
    *,
    source: str = DEFAULT_SOURCE,
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    notification_mode: NotificationMode | str = DEFAULT_NOTIFICATION_MODE,
    digest_hour_local: int = DEFAULT_DIGEST_HOUR_LOCAL,
    enabled: bool = True,
    now: datetime | None = None,
    next_run_at: datetime | None = None,
) -> dict[str, object]:
    """Create or replace the current athlete's automatic sync policy."""

    runtime = _HostRuntime.load(data_home)
    mode = (
        notification_mode
        if isinstance(notification_mode, NotificationMode)
        else NotificationMode(notification_mode)
    )
    subscription = SyncSubscription(
        athlete_id=runtime.athlete_id,
        source=source,
        interval_minutes=interval_minutes,
        lookback_days=lookback_days,
        notification_mode=mode,
        digest_hour_local=digest_hour_local,
        enabled=enabled,
    )
    scheduled_at = _aware(next_run_at or now or datetime.now(UTC), "next_run_at")
    runtime.automation.save_subscription(subscription, next_run_at=scheduled_at)
    return {
        "policy": _policy_view(subscription),
        "next_run_at": scheduled_at.isoformat(),
    }


def automation_status(
    data_home: str | Path,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Return operational state without credentials, leases, paths, or raw payloads."""

    runtime = _HostRuntime.load(data_home)
    checked_at = _aware(now or datetime.now(UTC), "now")
    policies: list[dict[str, object]] = []
    for subscription in runtime.automation.list_subscriptions(runtime.athlete_id):
        policy = _policy_view(subscription)
        policy["next_run_at"] = _datetime_text(
            runtime.automation.get_next_run_at(subscription.athlete_id, subscription.source)
        )
        policy["last_run"] = _run_view(
            runtime.automation.latest_sync_run(subscription.athlete_id, subscription.source)
        )
        policies.append(policy)

    snapshots = runtime.automation.list_snapshots(runtime.athlete_id)
    latest_snapshot = snapshots[-1] if snapshots else None
    notifications = runtime.automation.list_notifications(
        runtime.athlete_id,
        include_read=True,
    )
    due = [
        item
        for item in runtime.automation.due_subscriptions(checked_at)
        if item.athlete_id == runtime.athlete_id
    ]
    recoverable = [
        item
        for item in runtime.automation.list_recoverable_runs(checked_at)
        if item.athlete_id == runtime.athlete_id
    ]
    return {
        "athlete_id": runtime.athlete_id,
        "checked_at": checked_at.isoformat(),
        "policies": policies,
        "backlog": {
            "due_subscriptions": len(due),
            "recoverable_runs": len(recoverable),
            "unread_notifications": sum(item.read_at is None for item in notifications),
            "delivery_backlog": sum(item.delivered_at is None for item in notifications),
        },
        "last_analysis": (
            {
                "period": latest_snapshot.period,
                "risk_level": latest_snapshot.risk_level,
                "created_at": latest_snapshot.created_at.isoformat(),
            }
            if latest_snapshot is not None
            else None
        ),
    }


def run_automation_once(
    data_home: str | Path,
    *,
    force: bool = False,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    """Run one bounded deterministic sync/analyze/notify pass."""

    runtime = _HostRuntime.load(data_home)
    active_clock = clock or (lambda: datetime.now(UTC))
    loop = AutomaticTrainingLoop(
        automation_repository=runtime.automation,
        record_store=runtime.records,
        profile_repository=runtime.profiles,
        connectors={DEFAULT_SOURCE: GarminCnConnector(home=runtime.home)},
        raw_root=runtime.layout.raw,
        notification_sink=default_notification_sink(),
        clock=active_clock,
        owner="automation-host",
    )
    return asdict(loop.run_once(claim_reason="force" if force else "schedule"))


def list_notifications(
    data_home: str | Path,
    *,
    include_read: bool = False,
) -> dict[str, object]:
    runtime = _HostRuntime.load(data_home)
    notifications = runtime.automation.list_notifications(
        runtime.athlete_id,
        include_read=include_read,
    )
    return {"notifications": [_notification_view(item) for item in notifications]}


def read_notification(
    data_home: str | Path,
    notification_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    runtime = _HostRuntime.load(data_home)
    notification = runtime.automation.get_notification(notification_id)
    if notification is None or notification.athlete_id != runtime.athlete_id:
        raise KeyError("notification not found")
    marked = runtime.automation.mark_read(
        notification_id,
        read_at=_aware(now or datetime.now(UTC), "now"),
    )
    return {"notification": _notification_view(marked)}


def install_macos_automation(data_home: str | Path) -> dict[str, object]:
    home = Path(data_home).expanduser().resolve()
    return MacOSLaunchdScheduler(
        data_home=home,
        python_executable=Path(sys.executable),
    ).install()


def uninstall_macos_automation(data_home: str | Path) -> dict[str, object]:
    home = Path(data_home).expanduser().resolve()
    return MacOSLaunchdScheduler(
        data_home=home,
        python_executable=Path(sys.executable),
    ).uninstall()


def _policy_view(subscription: SyncSubscription) -> dict[str, object]:
    return {
        "source": subscription.source,
        "interval_minutes": subscription.interval_minutes,
        "lookback_days": subscription.lookback_days,
        "notification_mode": subscription.notification_mode.value,
        "digest_hour_local": subscription.digest_hour_local,
        "enabled": subscription.enabled,
    }


def _run_view(run: SyncRun | None) -> dict[str, object] | None:
    if run is None:
        return None
    error_class = None
    if run.error:
        match = _SANITIZED_SYNC_ERROR.fullmatch(run.error)
        candidate = match.group("error_class") if match is not None else None
        error_class = candidate if candidate in _SAFE_ERROR_CLASSES else "UnknownError"
    return {
        "status": run.status,
        "started_at": run.started_at.isoformat(),
        "completed_at": _datetime_text(run.completed_at),
        "error_class": error_class,
    }


def _notification_view(notification: UserNotification) -> dict[str, object]:
    return {
        "notification_id": notification.notification_id,
        "priority": notification.priority,
        "title": notification.title,
        "body": notification.body,
        "created_at": notification.created_at.isoformat(),
        "deliver_after": _datetime_text(notification.deliver_after),
        "read_at": _datetime_text(notification.read_at),
    }


def _datetime_text(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


# Command-aligned names are the stable host API. The verb-first spellings above
# remain available for callers that used the initial Python facade draft.
automation_configure = configure_automation
automation_run_once = run_automation_once
automation_install_macos = install_macos_automation
automation_uninstall_macos = uninstall_macos_automation
notification_list = list_notifications
notification_read = read_notification
