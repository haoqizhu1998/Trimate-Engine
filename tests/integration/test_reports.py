from __future__ import annotations

import unittest
from datetime import UTC, date, datetime
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


if __name__ == "__main__":
    unittest.main()
