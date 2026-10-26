from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from athlete_growth.adapters.storage import SQLiteRecordStore
from athlete_growth.application.ingest import service
from athlete_growth.ports import data_source


class IngestionTests(unittest.TestCase):
    def test_ingests_idempotently_and_preserves_raw_evidence(self) -> None:
        ingestion_type = getattr(service, "IngestionService", None)
        raw_type = getattr(data_source, "RawSourceRecord", None)
        batch_type = getattr(data_source, "SyncBatch", None)
        self.assertIsNotNone(ingestion_type)
        self.assertIsNotNone(raw_type)
        self.assertIsNotNone(batch_type)
        occurred_at = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
        batch = batch_type(
            records=(
                raw_type(
                    source="file",
                    kind="activity",
                    source_id="run-1",
                    occurred_at=occurred_at,
                    payload={"sport": "running", "duration_min": 30.0},
                ),
            ),
            next_cursor="cursor-1",
        )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteRecordStore(root / "athlete.sqlite3")
            ingestion = ingestion_type(athlete_id="athlete-001", store=store, raw_dir=root / "raw")

            first = ingestion.ingest(batch)
            second = ingestion.ingest(batch)

            self.assertEqual((first.inserted, first.duplicates), (1, 0))
            self.assertEqual((second.inserted, second.duplicates), (0, 1))
            stored = store.list_current("athlete-001", kind="activity")[0]
            self.assertEqual(stored.evidence[0].source_id, "run-1")
            self.assertTrue(any((root / "raw").rglob("*.json")))
            self.assertEqual(store.get_cursor("athlete-001", "file"), "cursor-1")


if __name__ == "__main__":
    unittest.main()
