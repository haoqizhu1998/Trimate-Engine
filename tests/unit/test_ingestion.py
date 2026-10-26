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

    def test_stores_raw_vendor_payload_and_uses_separate_canonical_payload(self) -> None:
        occurred_at = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
        batch = data_source.SyncBatch(
            records=(
                data_source.RawSourceRecord(
                    source="garmin-cn",
                    kind="activity",
                    source_id="run-1",
                    occurred_at=occurred_at,
                    payload={"vendorDuration": 1800, "privateVendorField": "kept-raw"},
                    canonical_payload={"duration_min": 30.0, "distance_m": 5000.0},
                ),
            )
        )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteRecordStore(root / "athlete.sqlite3")
            ingestion = service.IngestionService(
                athlete_id="athlete-001", store=store, raw_dir=root / "raw"
            )

            ingestion.ingest(batch)

            stored = store.list_current("athlete-001", kind="activity")[0]
            self.assertEqual(stored.data, {"duration_min": 30.0, "distance_m": 5000.0})
            raw_file = next((root / "raw").rglob("*.json"))
            self.assertIn("privateVendorField", raw_file.read_text(encoding="utf-8"))

    def test_new_normalization_version_replaces_current_source_record(self) -> None:
        occurred_at = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)

        def batch(duration_min: float) -> data_source.SyncBatch:
            return data_source.SyncBatch(
                records=(
                    data_source.RawSourceRecord(
                        source="garmin-cn",
                        kind="activity",
                        source_id="run-1",
                        occurred_at=occurred_at,
                        payload={"duration": 1800},
                        canonical_payload={"duration_min": duration_min},
                    ),
                )
            )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteRecordStore(root / "athlete.sqlite3")
            ingestion = service.IngestionService(
                athlete_id="athlete-001", store=store, raw_dir=root / "raw"
            )

            first = ingestion.ingest(batch(0.0))
            replacement = ingestion.ingest(batch(30.0))
            repeated = ingestion.ingest(batch(30.0))

            current = store.list_current("athlete-001", kind="activity")
            self.assertEqual(first.inserted, 1)
            self.assertEqual(replacement.updated, 1)
            self.assertEqual(repeated.updated, 0)
            self.assertEqual(repeated.duplicates, 1)
            self.assertEqual(len(current), 1)
            self.assertEqual(current[0].data["duration_min"], 30.0)
            self.assertIsNotNone(current[0].supersedes_id)

    def test_quality_flags_only_metrics_that_must_be_non_negative(self) -> None:
        flags = service.IngestionService._quality_flags(
            {
                "distance_m": -1.0,
                "difference_body_battery": -8,
                "skin_temperature_deviation_c": -0.5,
            }
        )

        self.assertEqual(flags, ("negative:distance_m",))

    def test_source_identity_includes_kind_when_vendor_reuses_daily_id(self) -> None:
        occurred_at = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
        records = (
            data_source.RawSourceRecord(
                source="garmin-cn",
                kind="daily-summary",
                source_id="2026-10-26",
                occurred_at=occurred_at,
                payload={"totalSteps": 9000},
                canonical_payload={"steps": 9000},
            ),
            data_source.RawSourceRecord(
                source="garmin-cn",
                kind="stress",
                source_id="2026-10-26",
                occurred_at=occurred_at,
                payload={"averageStressLevel": 25},
                canonical_payload={"average_stress": 25},
            ),
        )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteRecordStore(root / "athlete.sqlite3")
            ingestion = service.IngestionService(
                athlete_id="athlete-001", store=store, raw_dir=root / "raw"
            )

            first = ingestion.ingest(data_source.SyncBatch(records))
            second = ingestion.ingest(data_source.SyncBatch(records))

            current = store.list_current("athlete-001")
            self.assertEqual(first.inserted, 2)
            self.assertEqual(second.duplicates, 2)
            self.assertEqual({record.kind for record in current}, {"daily-summary", "stress"})

    def test_retires_duplicate_current_versions_for_same_source_identity(self) -> None:
        occurred_at = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
        evidence = data_source.RawSourceRecord(
            source="garmin-cn",
            kind="stress",
            source_id="2026-10-26",
            occurred_at=occurred_at,
            payload={"averageStressLevel": 25},
            canonical_payload={"average_stress": 25},
        )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteRecordStore(root / "athlete.sqlite3")
            for value in (24, 25):
                store.save(
                    service.CanonicalRecord.create(
                        athlete_id="athlete-001",
                        kind="stress",
                        occurred_at=occurred_at,
                        data={"average_stress": value},
                        evidence=(
                            service.EvidenceRef(
                                "garmin-cn", "2026-10-26", occurred_at, f"{value:064x}"
                            ),
                        ),
                    )
                )
            ingestion = service.IngestionService(
                athlete_id="athlete-001", store=store, raw_dir=root / "raw"
            )

            result = ingestion.ingest(data_source.SyncBatch((evidence,)))

            current = store.list_current("athlete-001", kind="stress")
            self.assertEqual(result.retired, 1)
            self.assertEqual(len(current), 1)
            self.assertEqual(current[0].data["average_stress"], 25)


if __name__ == "__main__":
    unittest.main()
