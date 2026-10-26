"""Data-source connector contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol

from athlete_growth.domain.records import JsonValue


@dataclass(frozen=True, slots=True)
class SourceCapabilities:
    source: str
    region: str | None
    kinds: tuple[str, ...]
    read_only: bool = True
    supports_write: bool = False
    delivery_mode: Literal["push", "poll", "manual"] = "manual"
    suggested_poll_minutes: int | None = None

    def __post_init__(self) -> None:
        if self.delivery_mode not in {"push", "poll", "manual"}:
            raise ValueError("delivery_mode must be 'push', 'poll', or 'manual'")
        if self.suggested_poll_minutes is None:
            return
        if isinstance(self.suggested_poll_minutes, bool) or not isinstance(
            self.suggested_poll_minutes, int
        ):
            raise TypeError("suggested_poll_minutes must be an integer")
        if self.suggested_poll_minutes <= 0:
            raise ValueError("suggested_poll_minutes must be positive")
        if self.delivery_mode != "poll":
            raise ValueError(
                "only poll sources may declare suggested_poll_minutes"
            )


@dataclass(frozen=True, slots=True)
class SyncWindow:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("sync end date must not precede start date")


@dataclass(frozen=True, slots=True)
class RawSourceRecord:
    source: str
    kind: str
    source_id: str
    occurred_at: datetime
    payload: dict[str, JsonValue]
    canonical_payload: dict[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class SyncBatch:
    records: tuple[RawSourceRecord, ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class HealthStatus:
    ok: bool
    message: str


class DataSourceConnector(Protocol):
    def capabilities(self) -> SourceCapabilities: ...

    def sync(self, window: SyncWindow, cursor: str | None) -> SyncBatch: ...

    def healthcheck(self) -> HealthStatus: ...

    def revoke(self) -> None: ...
