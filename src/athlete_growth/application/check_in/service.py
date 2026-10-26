"""Daily subjective check-ins and context events."""

from __future__ import annotations

from datetime import datetime

from athlete_growth.adapters.storage.sqlite_records import SQLiteRecordStore
from athlete_growth.domain.records import CanonicalRecord, JsonValue


class CheckInService:
    def __init__(self, athlete_id: str, store: SQLiteRecordStore) -> None:
        self.athlete_id = athlete_id
        self.store = store

    def record(
        self,
        *,
        occurred_at: datetime,
        fatigue: int,
        soreness: int,
        pain: int,
        mood: int,
        motivation: int,
        illness: bool,
        rpe: int | None = None,
        context: dict[str, JsonValue] | None = None,
    ) -> CanonicalRecord:
        scores = {
            "fatigue": fatigue,
            "soreness": soreness,
            "pain": pain,
            "mood": mood,
            "motivation": motivation,
        }
        if rpe is not None:
            scores["rpe"] = rpe
        if any(value < 0 or value > 10 for value in scores.values()):
            raise ValueError("check-in scores must be between 0 and 10")
        data: dict[str, JsonValue] = {
            **scores,
            "illness": illness,
            "context": context or {},
        }
        record = CanonicalRecord.create(
            athlete_id=self.athlete_id,
            kind="check-in",
            occurred_at=occurred_at,
            data=data,
        )
        self.store.save(record)
        return record

    def record_context(
        self, *, occurred_at: datetime, context_type: str, details: dict[str, JsonValue]
    ) -> CanonicalRecord:
        record = CanonicalRecord.create(
            athlete_id=self.athlete_id,
            kind="context-event",
            occurred_at=occurred_at,
            data={"context_type": context_type, "details": details},
        )
        self.store.save(record)
        return record
