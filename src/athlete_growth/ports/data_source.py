"""Data-source connector contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from athlete_growth.domain.records import JsonValue


@dataclass(frozen=True, slots=True)
class SourceCapabilities:
    source: str
    region: str | None
    kinds: tuple[str, ...]
    read_only: bool = True
    supports_write: bool = False


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
