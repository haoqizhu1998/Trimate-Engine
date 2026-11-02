"""Confirmed, private training preferences shared by growth and planning."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import cast

from athlete_growth.domain.records import CanonicalRecord, JsonValue
from athlete_growth.ports.repositories import CanonicalRecordRepository

PREFERENCE_KIND = "training-preference"
_FIELDS = {
    "key_session_pattern",
    "aerobic_mixing",
    "preferred_quality_weekday",
    "preferred_long_weekday",
    "focus_sport",
}
_SPORTS = {"running", "cycling", "swimming"}


def current_training_preferences(
    records: CanonicalRecordRepository,
    athlete_id: str,
) -> dict[str, object]:
    """Return only confirmed, structured profile preferences; never parse notes."""
    found = records.list_current(athlete_id, kind=PREFERENCE_KIND)
    if len(found) > 1:
        raise ValueError("multiple current training preference versions")
    if not found:
        return {"record_id": None, "preferences": {}, "source": "none"}
    record = found[0]
    value = record.data.get("preferences")
    if not isinstance(value, dict):
        raise ValueError("invalid training preference record")
    return {
        "record_id": record.record_id,
        "preferences": dict(value),
        "source": "user_confirmed",
        "confirmed_at": record.occurred_at.isoformat(),
    }


def _validate_changes(changes: dict[str, object]) -> dict[str, JsonValue]:
    if not changes or set(changes) - _FIELDS:
        raise ValueError("training preference fields are invalid")
    normalized: dict[str, JsonValue] = {}
    for key, value in changes.items():
        if value is None:
            normalized[key] = None
        elif key in {"preferred_quality_weekday", "preferred_long_weekday"}:
            if type(value) is not int or value not in range(7):
                raise ValueError("preference weekdays must be between 0 and 6")
            normalized[key] = value
        elif key == "aerobic_mixing":
            if type(value) is not bool:
                raise ValueError("aerobic_mixing preference must be boolean")
            normalized[key] = value
        elif key == "focus_sport":
            if not isinstance(value, str) or value not in _SPORTS:
                raise ValueError("focus_sport preference is invalid")
            normalized[key] = cast(str, value)
        elif key == "key_session_pattern":
            if not isinstance(value, str) or value not in {"focus_blocks", "mixed"}:
                raise ValueError("key_session_pattern preference is invalid")
            normalized[key] = cast(str, value)
    return normalized


class TrainingPreferenceService:
    def __init__(self, athlete_id: str, records: CanonicalRecordRepository) -> None:
        self.athlete_id = athlete_id
        self.records = records

    def get(self) -> dict[str, object]:
        return current_training_preferences(self.records, self.athlete_id)

    def _candidate(self, changes: dict[str, object]) -> tuple[dict[str, object], str]:
        validated = _validate_changes(changes)
        current = self.get()
        previous = current["preferences"]
        assert isinstance(previous, dict)
        proposed = dict(previous)
        for key, value in validated.items():
            if value is None:
                proposed.pop(key, None)
            else:
                proposed[key] = value
        payload = {
            "athlete_id": self.athlete_id,
            "current_record_id": current["record_id"],
            "changes": validated,
            "proposed": proposed,
            "schema_version": 1,
        }
        preview_hash = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {
            "current_record_id": current["record_id"],
            "current_preferences": previous,
            "changes": validated,
            "proposed_preferences": proposed,
            "changed": proposed != previous,
            "requires_confirmation": True,
            "application_limits": [
                "Use preview_plan_blocks for multiweek focus rotation; future weeks "
                "require a fresh preview and confirmation before execution."
            ]
            if proposed.get("key_session_pattern") == "focus_blocks"
            else [],
        }, preview_hash

    def preview(self, changes: dict[str, object]) -> dict[str, object]:
        candidate, preview_hash = self._candidate(changes)
        return {**candidate, "preview_hash": preview_hash, "saved": False}

    def confirm(self, changes: dict[str, object], preview_hash: str) -> dict[str, object]:
        candidate, expected_hash = self._candidate(changes)
        if preview_hash != expected_hash:
            current_id = candidate["current_record_id"]
            current = self.records.get(str(current_id)) if current_id else None
            if current is not None and current.data.get("confirmed_preview_hash") == preview_hash:
                return {
                    "saved": False,
                    "already_confirmed": True,
                    "record_id": current.record_id,
                    "preferences": current.data["preferences"],
                }
            raise ValueError("training preferences changed; preview them again")
        if not candidate["changed"]:
            return {**candidate, "saved": False, "record_id": candidate["current_record_id"]}
        prior_id = candidate["current_record_id"]
        record = CanonicalRecord.create(
            athlete_id=self.athlete_id,
            kind=PREFERENCE_KIND,
            occurred_at=datetime.now(UTC),
            data={
                "schema_version": 1,
                "preferences": cast(dict[str, JsonValue], candidate["proposed_preferences"]),
                "source": "user_confirmed",
                "confirmed_preview_hash": preview_hash,
            },
            supersedes_id=cast(str | None, prior_id),
        )
        saved = self.records.replace_singleton(
            self.athlete_id, PREFERENCE_KIND, cast(str | None, prior_id), record
        )
        return {
            "saved": True,
            "record_id": saved.record_id,
            "preferences": candidate["proposed_preferences"],
            "supersedes_record_id": prior_id,
            "application_limits": candidate["application_limits"],
        }
