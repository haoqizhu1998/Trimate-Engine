"""User-confirmed sport test references with provenance and bounded intensity bands."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, date, datetime
from typing import cast

from athlete_growth.application.performance.service import normalize_sport
from athlete_growth.domain.records import CanonicalRecord, JsonValue
from athlete_growth.ports.repositories import CanonicalRecordRepository

_METRICS = {
    "running": ("threshold_pace_min_per_km", 2.0, 15.0),
    "cycling": ("ftp_watts", 40.0, 800.0),
    "swimming": ("css_pace_min_per_100m", 0.5, 6.0),
}
_PROTOCOLS = {"field_test", "lab_test", "race_result", "user_estimate"}
CALIBRATION_VERSION = "sport-calibration/1.0"


def _kind(sport: str) -> str:
    if sport not in _METRICS:
        raise ValueError("calibration sport must be running, cycling or swimming")
    return f"sport-calibration:{sport}"


def _bands(sport: str, value: float) -> dict[str, dict[str, float]]:
    # Reference bands are broad orientation around a user-declared test result.
    # They are not a session prescription or a validated individual threshold.
    factors = (
        {"easy": (1.15, 1.35), "steady": (1.05, 1.15), "threshold": (0.98, 1.03)}
        if sport != "cycling"
        else {"easy": (0.55, 0.75), "steady": (0.76, 0.90), "threshold": (0.91, 1.05)}
    )
    return {
        label: {"min": round(value * low, 2), "max": round(value * high, 2)}
        for label, (low, high) in factors.items()
    }


class SportCalibrationService:
    def __init__(
        self,
        athlete_id: str,
        records: CanonicalRecordRepository,
        *,
        today: date | None = None,
    ) -> None:
        self.athlete_id = athlete_id
        self.records = records
        self.today = today or datetime.now(UTC).date()

    def get(self, sport: str) -> dict[str, object]:
        found = self.records.list_current(self.athlete_id, kind=_kind(sport))
        if len(found) > 1:
            raise ValueError("multiple current sport calibrations")
        if not found:
            return {"sport": sport, "status": "missing", "record_id": None}
        record = found[0]
        tested = date.fromisoformat(str(record.data["tested_on"]))
        age = (self.today - tested).days
        is_current = 0 <= age <= 120 and record.data.get("protocol") != "user_estimate"
        return {
            "sport": sport,
            "status": "current" if is_current else "unverified"
            if record.data.get("protocol") == "user_estimate" else "stale",
            "record_id": record.record_id,
            "metric": record.data["metric"],
            "value": record.data["value"],
            "tested_on": tested.isoformat(),
            "age_days": age,
            "protocol": record.data["protocol"],
            "source_activity_id": record.data.get("source_activity_id"),
            "confidence": "user_declared_with_activity" if record.data.get("source_activity_id")
            else "user_declared",
            "reference_bands": record.data["reference_bands"] if is_current else None,
            "method_version": CALIBRATION_VERSION,
            "interpretation": (
                "Broad orientation from a user-declared test; not a validated prescription. "
                "Recheck protocol and current readiness before using in a session."
            ),
        }

    def _candidate(
        self,
        sport: str,
        value: float,
        tested_on: date,
        protocol: str,
        source_activity_id: str | None,
    ) -> tuple[dict[str, object], str]:
        _kind(sport)
        metric, minimum, maximum = _METRICS[sport]
        if isinstance(value, bool) or not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError("calibration value is outside the supported reference range")
        if tested_on > self.today or (self.today - tested_on).days > 365:
            raise ValueError("test date must be within the past year")
        if protocol not in _PROTOCOLS:
            raise ValueError("unsupported test protocol")
        if source_activity_id is not None:
            activity = next(
                (
                    item for item in self.records.list_current(self.athlete_id, kind="activity")
                    if item.record_id == source_activity_id
                ),
                None,
            )
            if activity is None or normalize_sport(activity.data.get("sport")) != sport:
                raise ValueError("source activity must be a current activity in the same sport")
            if abs((activity.occurred_at.date() - tested_on).days) > 1:
                raise ValueError("source activity date must match the declared test date")
        prior = self.get(sport)
        proposed = {
            "sport": sport,
            "metric": metric,
            "value": round(value, 3),
            "tested_on": tested_on.isoformat(),
            "protocol": protocol,
            "source_activity_id": source_activity_id,
            "reference_bands": _bands(sport, value),
            "method_version": CALIBRATION_VERSION,
        }
        payload = {
            "athlete_id": self.athlete_id,
            "prior_record_id": prior["record_id"],
            "proposed": proposed,
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        return {
            "current": prior,
            "proposed": proposed,
            "current_record_id": prior["record_id"],
            "requires_confirmation": True,
            "interpretation": "User-declared test reference; bands are broad orientation only.",
        }, digest

    def preview(
        self,
        sport: str,
        value: float,
        tested_on: date,
        protocol: str,
        source_activity_id: str | None = None,
    ) -> dict[str, object]:
        candidate, digest = self._candidate(
            sport, value, tested_on, protocol, source_activity_id
        )
        return {**candidate, "preview_hash": digest, "saved": False}

    def confirm(
        self,
        sport: str,
        value: float,
        tested_on: date,
        protocol: str,
        preview_hash: str,
        source_activity_id: str | None = None,
    ) -> dict[str, object]:
        candidate, expected = self._candidate(
            sport, value, tested_on, protocol, source_activity_id
        )
        if preview_hash != expected:
            current_id = candidate["current_record_id"]
            current = self.records.get(str(current_id)) if current_id else None
            if current is not None and current.data.get("confirmed_preview_hash") == preview_hash:
                return {"saved": False, "already_confirmed": True, "record_id": current.record_id}
            raise ValueError("sport calibration changed; preview it again")
        prior_id = cast(str | None, candidate["current_record_id"])
        proposed = cast(dict[str, JsonValue], candidate["proposed"])
        record = CanonicalRecord.create(
            athlete_id=self.athlete_id,
            kind=_kind(sport),
            occurred_at=datetime.now(UTC),
            data={**proposed, "confirmed_preview_hash": preview_hash},
            supersedes_id=prior_id,
        )
        saved = self.records.replace_singleton(self.athlete_id, _kind(sport), prior_id, record)
        return {"saved": True, "record_id": saved.record_id, "calibration": self.get(sport)}
