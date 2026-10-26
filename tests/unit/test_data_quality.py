from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from athlete_growth.application.data_quality import inspect_records
from athlete_growth.domain.records import CanonicalRecord, EvidenceRef


class DataQualityTests(unittest.TestCase):
    def test_reports_source_conflicts_and_device_changes(self) -> None:
        occurred = datetime(2026, 10, 26, tzinfo=UTC)
        first = CanonicalRecord.create(
            athlete_id="athlete-001",
            kind="daily-summary",
            occurred_at=occurred,
            data={"steps": 8000, "device_id": "watch-a"},
            evidence=(EvidenceRef("garmin-cn", "a", occurred),),
        )
        conflict = CanonicalRecord.create(
            athlete_id="athlete-001",
            kind="daily-summary",
            occurred_at=occurred,
            data={"steps": 9000, "device_id": "watch-a"},
            evidence=(EvidenceRef("file", "b", occurred),),
        )
        changed = CanonicalRecord.create(
            athlete_id="athlete-001",
            kind="daily-summary",
            occurred_at=occurred + timedelta(days=1),
            data={"steps": 8500, "device_id": "watch-b"},
            evidence=(EvidenceRef("garmin-cn", "c", occurred + timedelta(days=1)),),
        )

        codes = {finding.code for finding in inspect_records([first, conflict, changed])}

        self.assertIn("source-conflict", codes)
        self.assertIn("device-change", codes)


if __name__ == "__main__":
    unittest.main()
