"""Immutable records used by the automatic data loop."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Never, Self, cast

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


class _FrozenJsonObject(dict[str, JsonValue]):
    """A JSON object that preserves dict compatibility without allowing mutation."""

    @staticmethod
    def _immutable() -> Never:
        raise TypeError("frozen JSON values cannot be mutated")

    def __setitem__(self, key: str, value: JsonValue) -> None:
        self._immutable()

    def __delitem__(self, key: str) -> None:
        self._immutable()

    def __ior__(self, value: object) -> Self:
        self._immutable()

    def clear(self) -> None:
        self._immutable()

    def pop(self, *args: object, **kwargs: object) -> JsonValue:
        self._immutable()

    def popitem(self) -> tuple[str, JsonValue]:
        self._immutable()

    def setdefault(self, key: str, default: JsonValue = None) -> JsonValue:
        self._immutable()

    def update(self, *args: object, **kwargs: JsonValue) -> None:
        self._immutable()


class _FrozenJsonArray(list[JsonValue]):
    """A JSON array that preserves list compatibility without allowing mutation."""

    @staticmethod
    def _immutable() -> Never:
        raise TypeError("frozen JSON values cannot be mutated")

    def __setitem__(
        self,
        index: object,
        value: object,
    ) -> None:
        self._immutable()

    def __delitem__(self, index: object) -> None:
        self._immutable()

    def __iadd__(self, values: Iterable[JsonValue]) -> Self:
        self._immutable()

    def __imul__(self, value: object) -> Self:
        self._immutable()

    def append(self, value: JsonValue) -> None:
        self._immutable()

    def clear(self) -> None:
        self._immutable()

    def extend(self, values: Iterable[JsonValue]) -> None:
        self._immutable()

    def insert(self, index: object, value: JsonValue) -> None:
        self._immutable()

    def pop(self, index: object = -1) -> JsonValue:
        self._immutable()

    def remove(self, value: JsonValue) -> None:
        self._immutable()

    def reverse(self) -> None:
        self._immutable()

    def sort(self, *args: object, **kwargs: object) -> None:
        self._immutable()


def _freeze_json(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return _FrozenJsonObject({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenJsonArray(_freeze_json(item) for item in value)
    return value


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
    decoded = cast(dict[str, JsonValue], json.loads(encoded))
    return _FrozenJsonObject({key: _freeze_json(item) for key, item in decoded.items()})


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
    request_key: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("run_id", "athlete_id", "source", "reason"):
            _require_text(getattr(self, field_name), field_name)
        if self.status not in {"running", "completed", "failed"}:
            raise ValueError("status must be running, completed, or failed")
        _require_aware(self.started_at, "started_at")
        if self.completed_at is not None:
            _require_aware(self.completed_at, "completed_at")
            if self.completed_at < self.started_at:
                raise ValueError("completed_at must not be earlier than started_at")
        if self.request_key is not None:
            _require_text(self.request_key, "request_key")
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


@dataclass(frozen=True, slots=True, eq=False)
class AnalysisSnapshot:
    snapshot_id: str
    change_set_id: str
    athlete_id: str
    period: str
    report: dict[str, JsonValue]
    risk_level: str
    created_at: datetime
    analysis_kind: str = "wellness"
    algorithm_version: str = "wellness-report/1.0"
    _report_json: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        for field_name in ("snapshot_id", "change_set_id", "athlete_id", "period"):
            _require_text(getattr(self, field_name), field_name)
        if self.risk_level not in {"unknown", "low", "moderate", "high"}:
            raise ValueError("risk_level must be unknown, low, moderate, or high")
        _require_text(self.analysis_kind, "analysis_kind")
        _require_text(self.algorithm_version, "algorithm_version")
        _require_aware(self.created_at, "created_at")
        report = _copy_json_object(self.report)
        object.__setattr__(self, "report", report)
        object.__setattr__(
            self,
            "_report_json",
            json.dumps(
                report,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AnalysisSnapshot):
            return NotImplemented
        return (
            self.snapshot_id,
            self.change_set_id,
            self.athlete_id,
            self.period,
            self._report_json,
            self.risk_level,
            self.created_at,
            self.analysis_kind,
            self.algorithm_version,
        ) == (
            other.snapshot_id,
            other.change_set_id,
            other.athlete_id,
            other.period,
            other._report_json,
            other.risk_level,
            other.created_at,
            other.analysis_kind,
            other.algorithm_version,
        )

    def __hash__(self) -> int:
        return hash(
            (
                self.snapshot_id,
                self.change_set_id,
                self.athlete_id,
                self.period,
                self._report_json,
                self.risk_level,
                self.created_at,
                self.analysis_kind,
                self.algorithm_version,
            )
        )


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
    policy_version: str = "notification-policy/1.0"
    channel: str = "inbox"
    read_at: datetime | None = None
    delivered_at: datetime | None = None
    delivery_attempts: int = 0
    last_error: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("notification_id", "snapshot_id", "athlete_id", "title", "body"):
            _require_text(getattr(self, field_name), field_name)
        if self.priority not in {"actionable", "digest"}:
            raise ValueError("priority must be actionable or digest")
        _require_text(self.policy_version, "policy_version")
        _require_text(self.channel, "channel")
        for field_name in ("deliver_after", "read_at", "delivered_at"):
            value = getattr(self, field_name)
            if value is not None:
                _require_aware(value, field_name)
        _require_aware(self.created_at, "created_at")
        if (
            isinstance(self.delivery_attempts, bool)
            or not isinstance(self.delivery_attempts, int)
            or self.delivery_attempts < 0
        ):
            raise ValueError("delivery_attempts must be a non-negative integer")
        if self.last_error is not None and not self.last_error.strip():
            raise ValueError("last_error must not be blank")
