from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from athlete_growth.adapters.storage import SQLiteRecordStore
from athlete_growth.application.report import service
from athlete_growth.domain.records import CanonicalRecord, EvidenceRef
from athlete_growth.domain.safety.rules import RiskLevel


class ReportServiceTests(unittest.TestCase):
    def test_daily_report_keeps_objective_and_subjective_evidence_separate(self) -> None:
        report_type = getattr(service, "ReportService", None)
        self.assertIsNotNone(report_type)
        occurred = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)
        evidence = EvidenceRef("garmin-cn", "2026-10-26", occurred, "d" * 64)

        with TemporaryDirectory() as directory:
            store = SQLiteRecordStore(Path(directory) / "athlete.sqlite3")
            store.save(
                CanonicalRecord.create(
                    athlete_id="athlete-001",
                    kind="daily-summary",
                    occurred_at=occurred,
                    data={"resting_hr": 52, "steps": 8000},
                    evidence=(evidence,),
                )
            )
            reports = report_type("athlete-001", store)

            report = reports.daily(date(2026, 10, 26))

            self.assertEqual(report.safety.level, RiskLevel.UNKNOWN)
            self.assertEqual(report.objective["resting_hr"], 52)
            self.assertIsNone(report.subjective)
            self.assertIn("daily check-in", report.data_gaps)
            self.assertIn("d" * 64, report.evidence_ids)

    def test_daily_report_uses_athlete_timezone_boundaries(self) -> None:
        local_midnight_in_utc = datetime(2026, 10, 25, 16, 0, tzinfo=UTC)

        with TemporaryDirectory() as directory:
            store = SQLiteRecordStore(Path(directory) / "athlete.sqlite3")
            store.save(
                CanonicalRecord.create(
                    athlete_id="athlete-001",
                    kind="daily-summary",
                    occurred_at=local_midnight_in_utc,
                    data={"calendar_date": "2026-10-26", "steps": 9000},
                )
            )
            reports = service.ReportService("athlete-001", store, timezone_name="Asia/Shanghai")

            report = reports.daily(date(2026, 10, 26))

            self.assertEqual(report.objective["steps"], 9000)
            self.assertNotIn("objective daily summary", report.data_gaps)

    def test_daily_report_exposes_typed_overnight_hrv_separately_from_sleep(self) -> None:
        on = date(2026, 10, 26)
        with TemporaryDirectory() as directory:
            store = SQLiteRecordStore(Path(directory) / "athlete.sqlite3")
            for days_ago in range(28):
                night = on - timedelta(days=days_ago)
                store.save(
                    CanonicalRecord.create(
                        athlete_id="athlete-001", kind="hrv",
                        occurred_at=datetime(night.year, night.month, night.day, tzinfo=UTC),
                        data={"calendar_date": night.isoformat(),
                              "last_night_avg_ms": 48 if days_ago < 7 else 60},
                    )
                )
            report = service.ReportService("athlete-001", store).daily(on)

        assert report.hrv_context is not None
        self.assertEqual(report.hrv_context["recent_vs_prior_pct"], -20.0)
        self.assertEqual(report.hrv_context["latest_night_date"], on.isoformat())
        self.assertNotIn("hrv", report.objective)
        self.assertEqual(report.safety.level, RiskLevel.UNKNOWN)

    def test_weekly_period_is_exactly_seven_athlete_local_dates(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteRecordStore(Path(directory) / "athlete.sqlite3")
            reports = service.ReportService("athlete-001", store, timezone_name="Asia/Shanghai")

            report = reports.weekly(date(2026, 10, 26))

        self.assertEqual(report.period, "2026-10-20/2026-10-26")

    def test_weekly_spring_dst_window_excludes_previous_local_date(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteRecordStore(Path(directory) / "athlete.sqlite3")
            for occurred_at, load in (
                (datetime(2026, 3, 4, 4, 30, tzinfo=UTC), 99),
                (datetime(2026, 3, 4, 5, 30, tzinfo=UTC), 11),
            ):
                store.save(
                    CanonicalRecord.create(
                        athlete_id="athlete-001",
                        kind="activity",
                        occurred_at=occurred_at,
                        data={"training_load": load, "duration_min": 30},
                    )
                )
            reports = service.ReportService("athlete-001", store, timezone_name="America/New_York")

            report = reports.weekly(date(2026, 3, 10))

        self.assertEqual(report.period, "2026-03-04/2026-03-10")
        self.assertEqual(report.objective["activity_count"], 1)
        self.assertEqual(report.analytics.metrics["sessions_7d"], 1)
        self.assertEqual(report.analytics.metrics["acute_load"], 11)

    def test_weekly_fall_dst_window_includes_first_local_hour_consistently(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteRecordStore(Path(directory) / "athlete.sqlite3")
            for occurred_at, load in (
                (datetime(2026, 10, 29, 3, 30, tzinfo=UTC), 99),
                (datetime(2026, 10, 29, 4, 30, tzinfo=UTC), 13),
            ):
                store.save(
                    CanonicalRecord.create(
                        athlete_id="athlete-001",
                        kind="activity",
                        occurred_at=occurred_at,
                        data={"training_load": load, "duration_min": 30},
                    )
                )
            reports = service.ReportService("athlete-001", store, timezone_name="America/New_York")

            report = reports.weekly(date(2026, 11, 4))

        self.assertEqual(report.period, "2026-10-29/2026-11-04")
        self.assertEqual(report.objective["activity_count"], 1)
        self.assertEqual(report.analytics.metrics["sessions_7d"], 1)
        self.assertEqual(report.analytics.metrics["acute_load"], 13)


if __name__ == "__main__":
    unittest.main()
