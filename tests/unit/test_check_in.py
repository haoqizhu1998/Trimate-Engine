from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from athlete_growth.adapters.storage import SQLiteRecordStore
from athlete_growth.application.check_in import service


class CheckInTests(unittest.TestCase):
    def test_records_subjective_state_and_context(self) -> None:
        recorder_type = getattr(service, "CheckInService", None)
        self.assertIsNotNone(recorder_type)
        occurred_at = datetime(2026, 10, 26, 7, 0, tzinfo=UTC)

        with TemporaryDirectory() as directory:
            store = SQLiteRecordStore(Path(directory) / "athlete.sqlite3")
            recorder = recorder_type("athlete-001", store)
            record = recorder.record(
                occurred_at=occurred_at,
                fatigue=4,
                soreness=3,
                pain=0,
                mood=7,
                motivation=8,
                illness=False,
                rpe=6,
                context={"work_stress": 5, "travel": False},
            )

            self.assertEqual(record.data["fatigue"], 4)
            self.assertEqual(record.data["context"]["work_stress"], 5)
            self.assertEqual(store.list_current("athlete-001", kind="check-in"), [record])

    def test_rejects_values_outside_zero_to_ten(self) -> None:
        recorder_type = getattr(service, "CheckInService", None)
        self.assertIsNotNone(recorder_type)

        with TemporaryDirectory() as directory:
            store = SQLiteRecordStore(Path(directory) / "athlete.sqlite3")
            recorder = recorder_type("athlete-001", store)
            with self.assertRaisesRegex(ValueError, "0 and 10"):
                recorder.record(
                    occurred_at=datetime.now(UTC),
                    fatigue=11,
                    soreness=0,
                    pain=0,
                    mood=5,
                    motivation=5,
                    illness=False,
                )


if __name__ == "__main__":
    unittest.main()
