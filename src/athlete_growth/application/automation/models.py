"""Immutable records used by the automatic data loop."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import cast

from athlete_growth.domain.records import JsonValue


class NotificationMode(StrEnum):
    ACTIONABLE_DIGEST = "actionable-digest"
    ALL = "all"
    OFF = "off"


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _require_positive_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")


def _copy_json_object(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("report must contain only JSON values") from exc
    return cast(dict[str, JsonValue], json.loads(encoded))


@dataclass(frozen=True, slots=True)
class SyncSubscription:
    athlete_id: str
    source: str
    interval_minutes: int
    lookback_days: int
    notification_mode: NotificationMode
    digest_hour_local: int
    enabled: bool

    def __post_init__(self) -> None:
        _require_text(self.athlete_id, "athlete_id")
        _require_text(self.source, "source")
        _require_positive_int(self.interval_minutes, "interval_minutes")
        _require_positive_int(self.lookback_days, "lookback_days")
        if not isinstance(self.notification_mode, NotificationMode):
            raise ValueError("notification_mode must be a NotificationMode")
        if (
            isinstance(self.digest_hour_local, bool)
            or not isinstance(self.digest_hour_local, int)
            or not 0 <= self.digest_hour_local <= 23
        ):
            raise ValueError("digest_hour_local must be between 0 and 23")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")


@dataclass(frozen=True, slots=True)
class SyncRun:
    run_id: str
    athlete_id: str
    source: str
    reason: str
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("run_id", "athlete_id", "source", "reason"):
            _require_text(getattr(self, field_name), field_name)
        if self.status not in {"running", "completed", "failed"}:
            raise ValueError("status must be running, completed, or failed")
        _require_aware(self.started_at, "started_at")
        if self.completed_at is not None:
            _require_aware(self.completed_at, "completed_at")
        if self.status == "running" and (self.completed_at is not None or self.error is not None):
            raise ValueError("a running sync cannot be completed or have an error")
        if self.status in {"completed", "failed"} and self.completed_at is None:
            raise ValueError("a finished sync must have completed_at")
        if self.status == "completed" and self.error is not None:
            raise ValueError("a completed sync cannot have an error")
        if self.status == "failed" and (self.error is None or not self.error.strip()):
            raise ValueError("a failed sync must have an error")


@dataclass(frozen=True, slots=True)
class DataChangeSet:
    change_set_id: str
    run_id: str
    athlete_id: str
    source: str
    record_ids: tuple[str, ...]
    affected_dates: tuple[date, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("change_set_id", "run_id", "athlete_id", "source"):
            _require_text(getattr(self, field_name), field_name)
        _require_aware(self.created_at, "created_at")
        record_ids = tuple(sorted(set(self.record_ids)))
        if not record_ids or any(not record_id.strip() for record_id in record_ids):
            raise ValueError("record_ids must contain non-blank identifiers")
        if any(
            isinstance(value, datetime) or not isinstance(value, date)
            for value in self.affected_dates
        ):
            raise ValueError("affected_dates must contain dates")
        object.__setattr__(self, "record_ids", record_ids)
        object.__setattr__(self, "affected_dates", tuple(sorted(set(self.affected_dates))))


@dataclass(frozen=True, slots=True)
class AnalysisSnapshot:
    snapshot_id: str
    change_set_id: str
    athlete_id: str
    period: str
    report: dict[str, JsonValue]
    risk_level: str
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("snapshot_id", "change_set_id", "athlete_id", "period"):
            _require_text(getattr(self, field_name), field_name)
        if self.risk_level not in {"unknown", "low", "moderate", "high"}:
            raise ValueError("risk_level must be unknown, low, moderate, or high")
        _require_aware(self.created_at, "created_at")
        object.__setattr__(self, "report", _copy_json_object(self.report))


@dataclass(frozen=True, slots=True)
class UserNotification:
    notification_id: str
    snapshot_id: str
    athlete_id: str
    priority: str
    title: str
    body: str
    deliver_after: datetime | None
    created_at: datetime
    read_at: datetime | None = None
    delivered_at: datetime | None = None
    delivery_attempts: int = 0
    last_error: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("notification_id", "snapshot_id", "athlete_id", "title", "body"):
            _require_text(getattr(self, field_name), field_name)
        if self.priority not in {"actionable", "digest"}:
            raise ValueError("priority must be actionable or digest")
        for field_name in ("deliver_after", "read_at", "delivered_at"):
            value = getattr(self, field_name)
            if value is not None:
                _require_aware(value, field_name)
        _require_aware(self.created_at, "created_at")
        if self.delivery_attempts < 0:
            raise ValueError("delivery_attempts must not be negative")
        if self.last_error is not None and not self.last_error.strip():
            raise ValueError("last_error must not be blank")
