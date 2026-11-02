from __future__ import annotations

import unittest
from pathlib import Path

from athlete_growth.adapters.files import importer

FIXTURES = Path(__file__).parents[1] / "fixtures"


class FileImporterTests(unittest.TestCase):
    def test_reads_csv_activity_with_typed_metrics(self) -> None:
        source_type = getattr(importer, "FileDataSource", None)
        self.assertIsNotNone(source_type)

        batch = source_type().read(FIXTURES / "activities.csv")

        self.assertEqual(len(batch.records), 1)
        record = batch.records[0]
        self.assertEqual(record.kind, "activity")
        self.assertEqual(record.payload["distance_m"], 5000.0)
        self.assertEqual(record.payload["avg_hr"], 145.0)

    def test_reads_tcx_activity(self) -> None:
        source_type = getattr(importer, "FileDataSource", None)
        self.assertIsNotNone(source_type)

        batch = source_type().read(FIXTURES / "activity.tcx")

        record = batch.records[0]
        self.assertEqual(record.payload["sport"], "running")
        self.assertEqual(record.payload["duration_min"], 30.0)
        self.assertEqual(record.payload["distance_m"], 5000.0)
        self.assertEqual(record.payload["lap_count"], 1)
        self.assertEqual(record.payload["laps"][0]["distance_m"], 5000.0)


if __name__ == "__main__":
    unittest.main()
