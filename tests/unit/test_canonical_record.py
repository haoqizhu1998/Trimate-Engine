from __future__ import annotations

import unittest
from datetime import UTC, datetime

from athlete_growth.domain import records


class CanonicalRecordTests(unittest.TestCase):
    def test_create_is_deterministic_and_preserves_evidence(self) -> None:
        record_type = getattr(records, "CanonicalRecord", None)
        evidence_type = getattr(records, "EvidenceRef", None)
        self.assertIsNotNone(record_type)
        self.assertIsNotNone(evidence_type)
        captured_at = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
        evidence = evidence_type(
            source="garmin-cn",
            source_id="activity-42",
            captured_at=captured_at,
            raw_hash="a" * 64,
        )

        first = record_type.create(
            athlete_id="athlete-001",
            kind="activity",
            occurred_at=captured_at,
            data={"distance_m": 5000.0, "sport": "running"},
            evidence=(evidence,),
        )
        second = record_type.create(
            athlete_id="athlete-001",
            kind="activity",
            occurred_at=captured_at,
            data={"sport": "running", "distance_m": 5000.0},
            evidence=(evidence,),
        )

        self.assertEqual(first.record_id, second.record_id)
        self.assertEqual(first.evidence, (evidence,))
        self.assertEqual(first.schema_version, 1)

    def test_rejects_naive_occurrence_time(self) -> None:
        record_type = getattr(records, "CanonicalRecord", None)
        self.assertIsNotNone(record_type)

        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            record_type.create(
                athlete_id="athlete-001",
                kind="activity",
                occurred_at=datetime(2026, 10, 26, 8, 0),
                data={},
            )


if __name__ == "__main__":
    unittest.main()
