"""Evidence-backed daily and weekly reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from athlete_growth.application.analyze.service import TrainingAnalytics
from athlete_growth.application.report.hrv import overnight_hrv_context
from athlete_growth.domain.analytics import AnalysisResult
from athlete_growth.domain.records import CanonicalRecord, JsonValue
from athlete_growth.domain.safety import SafetyAssessment, SafetyRules
from athlete_growth.ports.repositories import CanonicalRecordRepository


@dataclass(frozen=True, slots=True)
class WellnessReport:
    period: str
    objective: dict[str, JsonValue]
    subjective: dict[str, JsonValue] | None
    analytics: AnalysisResult
    safety: SafetyAssessment
    evidence_ids: tuple[str, ...]
    data_gaps: tuple[str, ...]
    confidence: float
    hrv_context: dict[str, JsonValue] | None = None
    algorithm_version: str = "wellness-report/1.1"


class ReportService:
    def __init__(
        self,
        athlete_id: str,
        store: CanonicalRecordRepository,
        *,
        timezone_name: str = "UTC",
        projection_record_ids: tuple[str, ...] | None = None,
    ) -> None:
        self.athlete_id = athlete_id
        self.store = store
        self.timezone = ZoneInfo(timezone_name)
        self.projection_record_ids = projection_record_ids
        self.analytics = TrainingAnalytics()
        self.safety_rules = SafetyRules()

    def daily(self, calendar_date: date) -> WellnessReport:
        start = datetime.combine(calendar_date, time.min, tzinfo=self.timezone).astimezone(UTC)
        end = datetime.combine(
            calendar_date + timedelta(days=1), time.min, tzinfo=self.timezone
        ).astimezone(UTC) - timedelta(microseconds=1)
        records = self._records(start=start, end=end)
        objective_records = [record for record in records if record.kind == "daily-summary"]
        check_ins = [record for record in records if record.kind == "check-in"]
        objective: dict[str, JsonValue] = {}
        for record in objective_records:
            objective.update(record.data)
        sleep_record = self._daily_sleep_record(calendar_date, start=start, end=end)
        if sleep_record is not None:
            sleep_seconds = self._sleep_duration_seconds(sleep_record)
            if sleep_seconds is not None:
                objective["sleep_seconds"] = sleep_seconds
        sleep_seconds = objective.get("sleep_seconds")
        if isinstance(sleep_seconds, (int, float)) and 0 < sleep_seconds <= 86400:
            objective["sleep_hours"] = round(sleep_seconds / 3600, 2)
        check_in = check_ins[-1] if check_ins else None
        subjective = dict(check_in.data) if check_in else None
        history_start = datetime.combine(
            calendar_date - timedelta(days=27), time.min, tzinfo=self.timezone
        ).astimezone(UTC)
        acute_start = datetime.combine(
            calendar_date - timedelta(days=6), time.min, tzinfo=self.timezone
        ).astimezone(UTC)
        history = self._records(start=history_start, end=end)
        hrv_context = overnight_hrv_context(history, on=calendar_date, timezone=self.timezone)
        analytics = self.analytics.analyze(
            history,
            as_of=end,
            history_start=history_start,
            acute_start=acute_start,
        )
        safety = self.safety_rules.assess(
            check_in=check_in,
            observations=objective_records,
        )
        evidence = tuple(
            dict.fromkeys(
                evidence.raw_hash or record.record_id
                for record in (records + [sleep_record] if sleep_record else records)
                for evidence in record.evidence
            )
        )
        hrv_evidence = hrv_context["evidence_ids"]
        assert isinstance(hrv_evidence, list)
        evidence = tuple(
            dict.fromkeys((*evidence, *(item for item in hrv_evidence if isinstance(item, str))))
        )
        gaps = list(analytics.data_gaps) + list(safety.data_gaps)
        if not objective_records:
            gaps.append("objective daily summary")
        if "sleep_hours" not in objective:
            gaps.append("sleep duration")
        confidence = min(analytics.confidence, 0.9 if check_in else 0.45)
        return WellnessReport(
            calendar_date.isoformat(),
            objective,
            subjective,
            analytics,
            safety,
            evidence,
            tuple(dict.fromkeys(gaps)),
            confidence,
            hrv_context,
        )

    def weekly(self, end_date: date) -> WellnessReport:
        start_date = end_date - timedelta(days=6)
        start = datetime.combine(start_date, time.min, tzinfo=self.timezone).astimezone(UTC)
        end = datetime.combine(
            end_date + timedelta(days=1), time.min, tzinfo=self.timezone
        ).astimezone(UTC) - timedelta(microseconds=1)
        records = self._records(start=start, end=end)
        history_start = datetime.combine(
            end_date - timedelta(days=27), time.min, tzinfo=self.timezone
        ).astimezone(UTC)
        history = self._records(start=history_start, end=end)
        hrv_context = overnight_hrv_context(history, on=end_date, timezone=self.timezone)
        analytics = self.analytics.analyze(
            history,
            as_of=end,
            history_start=history_start,
            acute_start=start,
        )
        latest_check_in = next(
            (record for record in reversed(records) if record.kind == "check-in"), None
        )
        observations = [record for record in records if record.kind == "daily-summary"]
        safety = self.safety_rules.assess(
            check_in=latest_check_in,
            observations=observations[-1:],
        )
        objective: dict[str, JsonValue] = {
            "activity_count": sum(record.kind == "activity" for record in records),
            "check_in_days": len(
                {
                    record.occurred_at.astimezone(self.timezone).date()
                    for record in records
                    if record.kind == "check-in"
                }
            ),
        }
        evidence = tuple(
            dict.fromkeys(
                item.raw_hash or record.record_id for record in records for item in record.evidence
            )
        )
        hrv_evidence = hrv_context["evidence_ids"]
        assert isinstance(hrv_evidence, list)
        evidence = tuple(
            dict.fromkeys((*evidence, *(item for item in hrv_evidence if isinstance(item, str))))
        )
        gaps = list(analytics.data_gaps) + list(safety.data_gaps)
        return WellnessReport(
            f"{start_date.isoformat()}/{end_date.isoformat()}",
            objective,
            dict(latest_check_in.data) if latest_check_in else None,
            analytics,
            safety,
            evidence,
            tuple(dict.fromkeys(gaps)),
            analytics.confidence,
            hrv_context,
        )

    def _records(self, *, start: datetime, end: datetime) -> list[CanonicalRecord]:
        if self.projection_record_ids is None:
            return self.store.list_current(self.athlete_id, start=start, end=end)
        return self.store.list_by_ids(
            self.athlete_id,
            self.projection_record_ids,
            start=start,
            end=end,
        )

    def _daily_sleep_record(
        self, calendar_date: date, *, start: datetime, end: datetime
    ) -> CanonicalRecord | None:
        # A night's record may be timestamped at sleep onset on the previous day.
        candidates = self._records(start=start - timedelta(days=1), end=end + timedelta(days=1))
        matching = [
            record
            for record in candidates
            if record.kind == "sleep"
            and self._sleep_duration_seconds(record) is not None
            and (
                record.data.get("calendar_date") == calendar_date.isoformat()
                or (
                    not record.data.get("calendar_date")
                    and record.occurred_at.astimezone(self.timezone).date() == calendar_date
                )
            )
        ]
        if not matching:
            return None
        return max(matching, key=lambda record: (record.occurred_at, record.record_id))

    @staticmethod
    def _sleep_duration_seconds(record: CanonicalRecord) -> float | None:
        seconds = record.data.get("sleep_seconds")
        if not isinstance(seconds, (int, float)):
            hours = record.data.get("sleep_hours")
            seconds = hours * 3600 if isinstance(hours, (int, float)) else None
        if isinstance(seconds, (int, float)) and 0 < seconds <= 86400:
            return float(seconds)
        return None
