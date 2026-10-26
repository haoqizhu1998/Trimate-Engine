from __future__ import annotations

import gc
import unittest
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from athlete_growth.adapters.storage import sqlite_records
from athlete_growth.domain.records import CanonicalRecord, EvidenceRef


class SQLiteRecordStoreTests(unittest.TestCase):
    def test_save_is_idempotent_and_lists_current_records(self) -> None:
        store_type = getattr(sqlite_records, "SQLiteRecordStore", None)
        self.assertIsNotNone(store_type)
        occurred_at = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
        record = CanonicalRecord.create(
            athlete_id="athlete-001",
            kind="activity",
            occurred_at=occurred_at,
            data={"training_load": 80.0},
            evidence=(EvidenceRef("file", "run.csv:2", occurred_at, "b" * 64),),
        )

        with TemporaryDirectory() as directory:
            store = store_type(Path(directory) / "athlete.sqlite3")
            self.assertTrue(store.save(record))
            self.assertFalse(store.save(record))
            self.assertEqual(store.list_current("athlete-001", kind="activity"), [record])

    def test_correction_preserves_history_and_replaces_current_record(self) -> None:
        store_type = getattr(sqlite_records, "SQLiteRecordStore", None)
        self.assertIsNotNone(store_type)
        occurred_at = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
        original = CanonicalRecord.create(
            athlete_id="athlete-001",
            kind="body-weight",
            occurred_at=occurred_at,
            data={"weight_kg": 70.0},
        )
        replacement = CanonicalRecord.create(
            athlete_id="athlete-001",
            kind="body-weight",
            occurred_at=occurred_at + timedelta(microseconds=1),
            data={"weight_kg": 69.5},
            supersedes_id=original.record_id,
        )

        with TemporaryDirectory() as directory:
            store = store_type(Path(directory) / "athlete.sqlite3")
            store.save(original)
            store.correct(original.record_id, replacement)

            self.assertEqual(
                store.list_current("athlete-001", kind="body-weight"),
                [replacement],
            )
            self.assertEqual(store.get(original.record_id), original)

    def test_database_operations_do_not_leak_connections(self) -> None:
        store_type = getattr(sqlite_records, "SQLiteRecordStore", None)
        self.assertIsNotNone(store_type)

        with TemporaryDirectory() as directory, warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            store = store_type(Path(directory) / "athlete.sqlite3")
            store.list_current("athlete-001")
            del store
            gc.collect()

        leaked = [warning for warning in caught if "unclosed database" in str(warning.message)]
        self.assertEqual(leaked, [])


if __name__ == "__main__":
    unittest.main()
