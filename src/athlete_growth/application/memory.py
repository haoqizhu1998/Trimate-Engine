"""Bounded, confirmable manual training memory for the current private profile."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta

from athlete_growth.domain.records import CanonicalRecord, JsonValue
from athlete_growth.ports.repositories import CanonicalRecordRepository

_SPORTS = {"running", "cycling", "swimming", "triathlon", "other"}
_KINDS = {"activity", "body_status", "goal_note", "preference"}


def _score(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10:
        raise ValueError(f"{name} must be an integer from 0 to 10")
    return value


def _positive(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a nonnegative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be a nonnegative number")
    return result


def _text(value: object, name: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValueError(f"{name} is invalid")
    return value.strip()


class TrainingMemoryService:
    def __init__(self, athlete_id: str, records: CanonicalRecordRepository) -> None:
        self.athlete_id = athlete_id
        self.records = records

    def _record(
        self, kind: str, occurred_at: datetime, fields: dict[str, object]
    ) -> CanonicalRecord:
        if kind not in _KINDS:
            raise ValueError("memory kind is invalid")
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("memory time must include a timezone")
        if occurred_at > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("memory time is in the future")
        if not isinstance(fields, dict):
            raise ValueError("memory fields are invalid")
        allowed = (
            {"sport", "duration_min", "distance_m", "avg_hr", "avg_power_w", "rpe", "note"}
            if kind == "activity"
            else {"fatigue", "pain", "soreness", "stress", "sleep_hours", "illness",
                  "pain_area", "note"}
            if kind == "body_status" else {"note"}
        )
        if set(fields) - allowed:
            raise ValueError("memory fields contain unsupported keys")
        data: dict[str, JsonValue] = {"origin": "manual"}
        if kind == "activity":
            sport = fields.get("sport")
            if sport not in _SPORTS:
                raise ValueError("sport is invalid")
            data["sport"] = str(sport)
            duration = (
                _positive(fields["duration_min"], "duration_min")
                if "duration_min" in fields else 0
            )
            distance = (
                _positive(fields["distance_m"], "distance_m")
                if "distance_m" in fields else 0
            )
            for key in ("duration_min", "distance_m", "avg_hr", "avg_power_w"):
                if key in fields:
                    data[key] = _positive(fields[key], key)
            if duration <= 0 and distance <= 0:
                raise ValueError("activity needs duration or distance")
            if "rpe" in fields:
                data["rpe"] = _score(fields["rpe"], "rpe")
        elif kind == "body_status":
            for key in ("fatigue", "pain", "soreness", "stress"):
                if key in fields:
                    data[key] = _score(fields[key], key)
            if "sleep_hours" in fields:
                hours = _positive(fields["sleep_hours"], "sleep_hours")
                if hours > 24:
                    raise ValueError("sleep_hours is invalid")
                data["sleep_hours"] = hours
            if "illness" in fields:
                if not isinstance(fields["illness"], bool):
                    raise ValueError("illness must be boolean")
                data["illness"] = fields["illness"]
            if "pain_area" in fields:
                data["pain_area"] = _text(fields["pain_area"], "pain_area", maximum=80)
            if len(data) == 1 and "note" not in fields:
                raise ValueError("body status needs an observation")
        else:
            data["memory_type"] = kind
        if "note" in fields:
            data["note"] = _text(fields["note"], "note")
        if kind in {"goal_note", "preference"} and "note" not in data:
            raise ValueError("memory note is required")
        record_kind = kind if kind in {"activity", "body_status"} else "context-event"
        return CanonicalRecord.create(
            athlete_id=self.athlete_id,
            kind=record_kind,
            occurred_at=occurred_at,
            data=data,
        )

    def _hash(self, record: CanonicalRecord) -> str:
        serialized = json.dumps(
            {"athlete_id": self.athlete_id, "record_id": record.record_id},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    def preview(
        self, kind: str, occurred_at: datetime, fields: dict[str, object]
    ) -> dict[str, object]:
        record = self._record(kind, occurred_at, fields)
        return {
            "kind": kind, "occurred_at": record.occurred_at.isoformat(),
            "fields": record.data, "preview_hash": self._hash(record),
            "already_recorded": self.records.get(record.record_id) is not None,
            "requires_confirmation": True,
            "saved": False,
        }

    def confirm(
        self, kind: str, occurred_at: datetime, fields: dict[str, object], preview_hash: str
    ) -> dict[str, object]:
        record = self._record(kind, occurred_at, fields)
        if not isinstance(preview_hash, str) or preview_hash != self._hash(record):
            raise ValueError("training memory changed; preview it again")
        saved = self.records.save(record)
        return {
            "record_id": record.record_id, "kind": kind,
            "saved": saved, "already_recorded": not saved,
            "trend_signals": self._trend_signals(),
            "interpretation": (
                "Partial body status does not clear readiness; use a full check-in "
                "for the daily safety assessment."
            ),
        }

    def list_recent(self, *, days: int = 28, limit: int = 30) -> dict[str, object]:
        if not 1 <= days <= 365 or not 1 <= limit <= 50:
            raise ValueError("memory window or limit is invalid")
        cutoff = datetime.now(UTC) - timedelta(days=days)
        records = [
            record for record in self.records.list_current(self.athlete_id)
            if record.occurred_at >= cutoff
            and (
                (
                    record.kind in {"activity", "body_status"}
                    and record.data.get("origin") == "manual"
                )
                or (
                    record.kind == "context-event"
                    and record.data.get("memory_type") in {"goal_note", "preference"}
                )
            )
        ]
        records.sort(key=lambda record: record.occurred_at, reverse=True)
        return {
            "items": [
                {"record_id": record.record_id, "kind": record.data.get("memory_type")
                 or record.kind, "occurred_at": record.occurred_at.isoformat(),
                 "fields": record.data}
                for record in records[:limit]
            ],
            "count": min(len(records), limit),
        }

    def _trend_signals(self) -> list[dict[str, object]]:
        now = datetime.now(UTC)
        recent = [
            record for record in self.records.list_current(self.athlete_id, kind="body_status")
            if now - timedelta(days=14) <= record.occurred_at <= now
        ]
        signals: list[dict[str, object]] = []
        areas: dict[str, list[str]] = {}
        for record in recent:
            area = record.data.get("pain_area")
            pain = record.data.get("pain")
            if isinstance(area, str) and isinstance(pain, int) and pain > 0:
                areas.setdefault(area.casefold(), []).append(record.record_id)
        for area, ids in areas.items():
            if len(ids) >= 2:
                signals.append({
                    "type": "repeated_pain_area", "area": area,
                    "evidence_ids": ids[-3:],
                    "message": "同一部位近期重复报告疼痛，请降低相关训练并观察变化。",
                })
        fatigue_days = {
            record.occurred_at.date()
            for record in recent
            if isinstance((fatigue := record.data.get("fatigue")), int)
            and fatigue >= 7
        }
        for day in fatigue_days:
            if all(day - timedelta(days=index) in fatigue_days for index in range(3)):
                signals.append({
                    "type": "three_day_fatigue",
                    "message": "连续三天记录到较高疲劳，下一次训练宜保守。",
                })
                break
        return signals
