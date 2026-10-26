"""Deterministic personal training analytics."""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import mean

from athlete_growth.domain.analytics import AnalysisResult
from athlete_growth.domain.records import CanonicalRecord


class TrainingAnalytics:
    algorithm_version = "training-analytics/1.0"

    def analyze(self, records: list[CanonicalRecord], *, as_of: datetime) -> AnalysisResult:
        window_records = [
            record
            for record in records
            if as_of - timedelta(days=28) <= record.occurred_at <= as_of
        ]
        activities = [record for record in window_records if record.kind == "activity"]
        acute = [record for record in activities if record.occurred_at >= as_of - timedelta(days=7)]
        acute_load = sum(self._load(record) for record in acute)
        chronic_total = sum(self._load(record) for record in activities)
        has_baseline_span = bool(activities) and min(
            record.occurred_at for record in activities
        ) <= as_of - timedelta(days=14)
        chronic_weekly = chronic_total / 4.0 if has_baseline_span else None
        ratio = (
            acute_load / chronic_weekly
            if chronic_weekly is not None and chronic_weekly > 0
            else None
        )
        duration = sum(self._number(record.data.get("duration_min")) for record in acute)
        high_minutes = sum(self._number(record.data.get("high_intensity_min")) for record in acute)
        drift_values = [
            (second - first) / first * 100
            for record in acute
            if (first := self._optional_number(record.data.get("first_half_hr")))
            and (second := self._optional_number(record.data.get("second_half_hr")))
        ]
        resting_hr = self._values(
            window_records, {"daily-summary", "heart-rate"}, ("resting_hr", "restingHeartRate")
        )
        hrv = self._values(window_records, {"hrv"}, ("hrv", "hrv_ms", "lastNightAvg"))
        sleep_hours = self._values(
            window_records,
            {"sleep"},
            ("sleep_hours", "total_sleep_hours"),
        )
        sleep_seconds = self._values(
            window_records,
            {"sleep"},
            ("sleep_seconds", "totalSleepSeconds"),
        )
        sleep_hours.extend(value / 3600 for value in sleep_seconds)
        body_battery = self._values(
            window_records,
            {"daily-summary", "body-battery"},
            ("body_battery", "bodyBatteryMostRecentValue"),
        )
        weight_records = [record for record in window_records if record.kind == "body-weight"]
        weight_values = [
            (record.occurred_at, value)
            for record in weight_records
            if (value := self._first_number(record, ("weight_kg", "weight", "weightKilograms")))
            is not None
        ]
        metrics: dict[str, float | int | None] = {
            "acute_load": round(acute_load, 2),
            "chronic_weekly_load": round(chronic_weekly, 2) if chronic_weekly is not None else None,
            "acute_chronic_ratio": round(ratio, 3) if ratio is not None else None,
            "sessions_7d": len(acute),
            "duration_min_7d": round(duration, 2),
            "high_intensity_pct": round(high_minutes / duration * 100, 2) if duration > 0 else None,
            "consistency_days_28d": len({record.occurred_at.date() for record in activities}),
            "hr_drift_pct": round(mean(drift_values), 2) if drift_values else None,
            "resting_hr_baseline_28d": round(mean(resting_hr), 2) if resting_hr else None,
            "hrv_baseline_28d": round(mean(hrv), 2) if hrv else None,
            "sleep_hours_baseline_28d": round(mean(sleep_hours), 2) if sleep_hours else None,
            "body_battery_baseline_28d": round(mean(body_battery), 2) if body_battery else None,
            "weight_kg_latest": round(max(weight_values)[1], 2) if weight_values else None,
        }
        gaps: list[str] = []
        if not activities:
            gaps.append("training activities")
        if not drift_values:
            gaps.append("heart-rate drift")
        if chronic_weekly is None:
            gaps.append("28-day load baseline")
        for values, label in (
            (resting_hr, "resting heart rate baseline"),
            (hrv, "HRV baseline"),
            (sleep_hours, "sleep baseline"),
            (body_battery, "body battery baseline"),
            (weight_values, "body weight"),
        ):
            if not values:
                gaps.append(label)
        evidence = tuple(
            dict.fromkeys(
                item.raw_hash or record.record_id
                for record in window_records
                for item in (record.evidence or ())
            )
        )
        if not evidence:
            evidence = tuple(record.record_id for record in window_records)
        confidence = max(0.0, min(1.0, 0.35 + len(window_records) / 28.0))
        return AnalysisResult(
            metrics,
            evidence,
            tuple(gaps),
            round(confidence, 2),
            self.algorithm_version,
        )

    @staticmethod
    def _load(record: CanonicalRecord) -> float:
        direct = TrainingAnalytics._optional_number(record.data.get("training_load"))
        if direct is not None:
            return direct
        duration = TrainingAnalytics._number(record.data.get("duration_min"))
        rpe = TrainingAnalytics._optional_number(record.data.get("rpe"))
        return duration * rpe if rpe is not None else 0.0

    @staticmethod
    def _number(value: object) -> float:
        return float(value) if isinstance(value, (int, float)) else 0.0

    @staticmethod
    def _optional_number(value: object) -> float | None:
        return float(value) if isinstance(value, (int, float)) else None

    @classmethod
    def _values(
        cls,
        records: list[CanonicalRecord],
        kinds: set[str],
        keys: tuple[str, ...],
    ) -> list[float]:
        return [
            value
            for record in records
            if record.kind in kinds and (value := cls._first_number(record, keys)) is not None
        ]

    @classmethod
    def _first_number(cls, record: CanonicalRecord, keys: tuple[str, ...]) -> float | None:
        for key in keys:
            value = cls._optional_number(record.data.get(key))
            if value is not None:
                return value
        return None
