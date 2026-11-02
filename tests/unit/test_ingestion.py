from __future__ import annotations

import hashlib
import io
import unittest
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from athlete_growth.adapters.garmin_cn.connector import (
    GarminCnConnector,
    GarminCnSessionGateway,
    activity_detail_coverage,
    known_activity_summaries,
)
from athlete_growth.adapters.storage import SQLiteRecordStore
from athlete_growth.application.ingest import service
from athlete_growth.ports import data_source


class IngestionTests(unittest.TestCase):
    def test_garmin_activity_details_and_original_file_are_private_and_idempotent(self) -> None:
        source = {
            "activityId": 42,
            "activityName": "Morning Run",
            "activityType": {"typeKey": "running"},
            "startTimeGMT": "2026-10-25 23:30:00",
            "startTimeLocal": "2026-10-26 07:30:00",
            "duration": 1800.0,
            "distance": 5000.0,
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("activity.fit", b"fit-recording")
        original = buffer.getvalue()

        class FakeClient:
            detail_calls = 0

            def login(self, tokenstore: str) -> tuple[object, object]:
                return object(), object()

            def get_activities_by_date(
                self, startdate: str, enddate: str
            ) -> list[dict[str, object]]:
                return [source]

            def get_activity(self, activity_id: str) -> dict[str, object]:
                self.detail_calls += 1
                return {**source, "averageHR": 145}

            def get_activity_details(self, activity_id: str) -> dict[str, object]:
                return {"activityDetailMetrics": [{"metrics": [145]}]}

            def get_activity_laps(self, activity_id: str) -> dict[str, object]:
                return {"lapDTOs": [
                    {"duration": 900.0, "distance": 2500.0, "averageHR": 140},
                    {"duration": 900.0, "distance": 2500.0, "averageHR": 150},
                ]}

            def download_activity(self, activity_id: str, dl_fmt: object) -> bytes:
                return original

            def __getattr__(self, name: str):
                return lambda *args: []

        with TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient()
            gateway = GarminCnSessionGateway(
                token_dir=root / "tokens", client_factory=lambda **kwargs: client
            )
            connector = GarminCnConnector(home=root, gateway=gateway)
            batch = connector.sync(
                data_source.SyncWindow(date(2026, 10, 26), date(2026, 10, 26)), None
            )
            activity = next(record for record in batch.records if record.kind == "activity")
            self.assertEqual(activity.canonical_payload["detail_status"], "complete")
            self.assertEqual(activity.canonical_payload["recorded_sample_count"], 1)
            self.assertEqual(activity.canonical_payload["lap_count"], 2)
            self.assertEqual(activity.canonical_payload["avg_hr"], 145)
            self.assertEqual(len(activity.artifacts), 1)

            store = SQLiteRecordStore(root / "athlete.sqlite3")
            ingestion = service.IngestionService(
                athlete_id="athlete-001", store=store, raw_dir=root / "raw"
            )
            first = ingestion.ingest(batch)
            second = ingestion.ingest(batch)
            self.assertEqual((first.inserted, first.raw_files), (1, 2))
            self.assertEqual((second.duplicates, second.raw_files), (1, 0))
            stored = store.list_current("athlete-001", kind="activity")
            self.assertEqual(len(stored), 1)
            self.assertEqual(
                activity_detail_coverage(stored),
                {"complete": 1, "partial": 0, "summary_only": 0},
            )
            artifact_hash = hashlib.sha256(original).hexdigest()
            artifact_path = (
                root / "raw" / "garmin-cn" / "assets" / artifact_hash[:2]
                / f"{artifact_hash}.zip"
            )
            self.assertEqual(artifact_path.read_bytes(), original)
            self.assertEqual(artifact_path.stat().st_mode & 0o777, 0o600)

            cached_client = FakeClient()
            cached_gateway = GarminCnSessionGateway(
                token_dir=root / "tokens",
                client_factory=lambda **kwargs: cached_client,
                known_activity_summaries=known_activity_summaries(stored),
            )
            self.assertFalse(any(
                row["kind"] == "activity"
                for row in cached_gateway.fetch(date(2026, 10, 26), date(2026, 10, 26))
            ))
            self.assertEqual(cached_client.detail_calls, 0)

            class FailingRefreshClient(FakeClient):
                def get_activity_details(self, activity_id: str) -> dict[str, object]:
                    raise RuntimeError("temporary failure")

            source["activityName"] = "Renamed Morning Run"
            failing_gateway = GarminCnSessionGateway(
                token_dir=root / "tokens",
                client_factory=lambda **kwargs: FailingRefreshClient(),
                known_activity_summaries=known_activity_summaries(stored),
            )
            self.assertFalse(any(
                row["kind"] == "activity"
                for row in failing_gateway.fetch(date(2026, 10, 26), date(2026, 10, 26))
            ))

    def test_garmin_missing_detail_is_explicit_and_not_cached_as_complete(self) -> None:
        class PartialClient:
            def get_activity(self, activity_id: str) -> dict[str, object]:
                return {"activityId": 42}

            def get_activity_details(self, activity_id: str) -> dict[str, object]:
                raise RuntimeError("unavailable")

            def download_activity(self, activity_id: str, dl_fmt: object) -> bytes:
                return b"not-a-zip"

            def __getattr__(self, name: str):
                return lambda *args: []

        source = {"activityId": 42, "startTimeGMT": "2026-10-26 08:00:00"}
        row = GarminCnSessionGateway._activity_row(
            PartialClient(), source, date(2026, 10, 26), 0, "summary-digest"
        )
        activity = GarminCnConnector._to_raw(row)
        self.assertEqual(activity.canonical_payload["detail_status"], "partial")
        self.assertEqual(activity.artifacts, ())
        self.assertIn("details", activity.canonical_payload["detail_missing_sections"])
        self.assertIn("original_fit_zip", activity.canonical_payload["detail_missing_sections"])

    def test_rejects_mismatched_original_file_hash_before_persisting(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ingestion = service.IngestionService(
                athlete_id="athlete-001",
                store=SQLiteRecordStore(root / "athlete.sqlite3"),
                raw_dir=root / "raw",
            )
            batch = data_source.SyncBatch((data_source.RawSourceRecord(
                source="garmin-cn",
                kind="activity",
                source_id="42",
                occurred_at=datetime(2026, 10, 26, tzinfo=UTC),
                payload={"_sync": {"original_fit_zip_sha256": "wrong"}},
                artifacts=(data_source.RawSourceArtifact("fit-zip", b"bytes"),),
            ),))
            with self.assertRaisesRegex(ValueError, "artifact hash"):
                ingestion.ingest(batch)
            self.assertEqual(list((root / "raw").rglob("*")), [])

    def test_raw_source_rejects_unsafe_path_components(self) -> None:
        for source in (".", "..", "../escaped", "/absolute", "nested/source", r"nested\source"):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "source"):
                data_source.RawSourceRecord(
                    source=source,
                    kind="activity",
                    source_id="run-1",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    payload={"duration_min": 30},
                )

    def test_ingestion_rejects_staging_outside_raw_root_without_writing(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            raw_root = root / "raw"
            outside = root / "outside-staging"
            ingestion = service.IngestionService(
                athlete_id="athlete-001",
                store=SQLiteRecordStore(root / "athlete.sqlite3"),
                raw_dir=raw_root,
            )
            batch = data_source.SyncBatch(
                (
                    data_source.RawSourceRecord(
                        source="file",
                        kind="activity",
                        source_id="run-1",
                        occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                        payload={"duration_min": 30},
                    ),
                )
            )

            with self.assertRaisesRegex(ValueError, "raw root"):
                ingestion.ingest(batch, raw_staging_dir=outside)

            self.assertFalse(outside.exists())
            self.assertFalse(raw_root.exists())

    def test_ingestion_rejects_source_symlink_escape_without_writing(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            raw_root = root / "raw"
            outside = root / "outside"
            raw_root.mkdir()
            outside.mkdir()
            (raw_root / "file").symlink_to(outside, target_is_directory=True)
            ingestion = service.IngestionService(
                athlete_id="athlete-001",
                store=SQLiteRecordStore(root / "athlete.sqlite3"),
                raw_dir=raw_root,
            )
            batch = data_source.SyncBatch(
                (
                    data_source.RawSourceRecord(
                        source="file",
                        kind="activity",
                        source_id="run-1",
                        occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                        payload={"duration_min": 30},
                    ),
                )
            )

            with self.assertRaisesRegex(ValueError, "raw root"):
                ingestion.ingest(batch)

            self.assertEqual(list(outside.rglob("*.json")), [])

    def test_syncs_long_ranges_in_restart_safe_chunks(self) -> None:
        class RecordingConnector:
            def __init__(self) -> None:
                self.windows: list[data_source.SyncWindow] = []

            def capabilities(self) -> data_source.SourceCapabilities:
                return data_source.SourceCapabilities("garmin-cn", "cn", ("daily-summary",))

            def sync(
                self, window: data_source.SyncWindow, cursor: str | None
            ) -> data_source.SyncBatch:
                self.windows.append(window)
                occurred_at = datetime.combine(window.start, datetime.min.time(), tzinfo=UTC)
                return data_source.SyncBatch(
                    (
                        data_source.RawSourceRecord(
                            source="garmin-cn",
                            kind="daily-summary",
                            source_id=window.start.isoformat(),
                            occurred_at=occurred_at,
                            payload={"calendarDate": window.start.isoformat()},
                        ),
                    ),
                    next_cursor=window.end.isoformat(),
                )

            def healthcheck(self) -> data_source.HealthStatus:
                return data_source.HealthStatus(True, "connected")

            def revoke(self) -> None:
                return None

        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteRecordStore(root / "athlete.sqlite3")
            ingestion = service.IngestionService(
                athlete_id="athlete-001", store=store, raw_dir=root / "raw"
            )
            connector = RecordingConnector()
            window = data_source.SyncWindow(date(2026, 1, 1), date(2026, 3, 1))

            first = ingestion.sync_chunked(connector, window, chunk_days=28)
            second = ingestion.sync_chunked(connector, window, chunk_days=28)

            self.assertEqual(
                connector.windows[:3],
                [
                    data_source.SyncWindow(date(2026, 1, 1), date(2026, 1, 28)),
                    data_source.SyncWindow(date(2026, 1, 29), date(2026, 2, 25)),
                    data_source.SyncWindow(date(2026, 2, 26), date(2026, 3, 1)),
                ],
            )
            self.assertEqual((first.inserted, first.duplicates), (3, 0))
            self.assertEqual((second.inserted, second.duplicates), (0, 3))
            self.assertEqual(store.get_cursor("athlete-001", "garmin-cn"), "2026-03-01")

    def test_chunked_sync_unions_and_sorts_change_metadata(self) -> None:
        class MetadataIngestionService(service.IngestionService):
            def __init__(self) -> None:
                self.results = iter(
                    (
                        service.IngestionResult(
                            1,
                            0,
                            0,
                            0,
                            1,
                            changed_record_ids=("record-b", "record-a"),
                            affected_dates=(date(2026, 1, 2), date(2026, 1, 1)),
                        ),
                        service.IngestionResult(
                            0,
                            1,
                            1,
                            0,
                            1,
                            changed_record_ids=("record-c", "record-a"),
                            affected_dates=(date(2026, 1, 3), date(2026, 1, 1)),
                        ),
                    )
                )

            def sync(
                self,
                connector: data_source.DataSourceConnector,
                window: data_source.SyncWindow,
                *,
                max_attempts: int = 3,
            ) -> service.IngestionResult:
                return next(self.results)

        ingestion = MetadataIngestionService()

        result = ingestion.sync_chunked(
            object(),  # type: ignore[arg-type]
            data_source.SyncWindow(date(2026, 1, 1), date(2026, 1, 2)),
            chunk_days=1,
        )

        self.assertEqual(
            (result.inserted, result.updated, result.retired, result.raw_files),
            (1, 1, 1, 2),
        )
        self.assertEqual(
            result.changed_record_ids,
            ("record-a", "record-b", "record-c"),
        )
        self.assertEqual(
            result.affected_dates,
            (date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)),
        )
        self.assertTrue(result.has_changes)

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
            self.assertEqual(first.changed_record_ids, (stored.record_id,))
            self.assertEqual(first.affected_dates, (date(2026, 10, 26),))
            self.assertTrue(first.has_changes)
            self.assertEqual(second.changed_record_ids, ())
            self.assertEqual(second.affected_dates, ())
            self.assertFalse(second.has_changes)
            self.assertEqual(stored.evidence[0].source_id, "run-1")
            self.assertTrue(any((root / "raw").rglob("*.json")))
            self.assertEqual(store.get_cursor("athlete-001", "file"), "cursor-1")

    def test_garmin_daily_record_uses_canonical_date_across_utc_boundary(self) -> None:
        raw = GarminCnConnector._to_raw(
            {
                "kind": "daily-summary",
                "source_id": "2026-10-26",
                "occurred_at": "2026-10-26T00:00:00+08:00",
                "payload": {"calendarDate": "2026-10-26", "totalSteps": 9000},
            }
        )
        self.assertEqual(raw.occurred_at, datetime(2026, 10, 25, 16, 0, tzinfo=UTC))

        with TemporaryDirectory() as directory:
            root = Path(directory)
            ingestion = service.IngestionService(
                athlete_id="athlete-001",
                store=SQLiteRecordStore(root / "athlete.sqlite3"),
                raw_dir=root / "raw",
            )

            result = ingestion.ingest(data_source.SyncBatch((raw,)))

        self.assertEqual(result.affected_dates, (date(2026, 10, 26),))

    def test_garmin_early_morning_activity_uses_source_local_affected_date(self) -> None:
        row = GarminCnSessionGateway._row(
            "activity",
            {
                "activityId": 42,
                "activityName": "Morning Run",
                "startTimeGMT": "2026-10-25 23:30:00",
                "startTimeLocal": "2026-10-26 07:30:00",
                "duration": 1800.0,
            },
            date(2026, 10, 26),
            0,
        )
        raw = GarminCnConnector._to_raw(row)
        self.assertEqual(raw.occurred_at, datetime(2026, 10, 25, 23, 30, tzinfo=UTC))

        with TemporaryDirectory() as directory:
            root = Path(directory)
            ingestion = service.IngestionService(
                athlete_id="athlete-001",
                store=SQLiteRecordStore(root / "athlete.sqlite3"),
                raw_dir=root / "raw",
            )

            result = ingestion.ingest(data_source.SyncBatch((raw,)))

        self.assertEqual(result.affected_dates, (date(2026, 10, 26),))

    def test_source_capabilities_validate_poll_interval(self) -> None:
        poll = data_source.SourceCapabilities(
            "garmin-cn",
            "cn",
            ("daily-summary",),
            delivery_mode="poll",
            suggested_poll_minutes=30,
        )

        self.assertEqual(poll.suggested_poll_minutes, 30)
        with self.assertRaisesRegex(ValueError, "positive"):
            data_source.SourceCapabilities(
                "garmin-cn",
                "cn",
                ("daily-summary",),
                delivery_mode="poll",
                suggested_poll_minutes=0,
            )
        with self.assertRaisesRegex(ValueError, "only poll sources"):
            data_source.SourceCapabilities(
                "file",
                None,
                ("activity",),
                delivery_mode="manual",
                suggested_poll_minutes=30,
            )

    def test_source_capabilities_reject_unknown_delivery_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "delivery_mode"):
            data_source.SourceCapabilities(
                "future-source",
                None,
                ("activity",),
                delivery_mode="stream",  # type: ignore[arg-type]
            )

    def test_source_capabilities_require_integer_poll_interval(self) -> None:
        for invalid_interval in (True, "30"):
            with (
                self.subTest(invalid_interval=invalid_interval),
                self.assertRaisesRegex(TypeError, "integer"),
            ):
                data_source.SourceCapabilities(
                    "garmin-cn",
                    "cn",
                    ("daily-summary",),
                    delivery_mode="poll",
                    suggested_poll_minutes=invalid_interval,  # type: ignore[arg-type]
                )

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
            self.assertEqual(replacement.changed_record_ids, (current[0].record_id,))
            self.assertEqual(replacement.affected_dates, (date(2026, 10, 26),))
            self.assertTrue(replacement.has_changes)
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

    def test_daily_range_record_retires_legacy_intraday_duplicates(self) -> None:
        occurred_at = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)
        legacy = data_source.RawSourceRecord(
            source="garmin-cn",
            kind="steps",
            source_id="2026-10-26:steps:0",
            occurred_at=occurred_at,
            payload={"steps": 100},
            canonical_payload={"calendar_date": None, "steps": 100},
        )
        daily = data_source.RawSourceRecord(
            source="garmin-cn",
            kind="steps",
            source_id="2026-10-26",
            occurred_at=occurred_at,
            payload={"calendarDate": "2026-10-26", "totalSteps": 9000},
            canonical_payload={"calendar_date": "2026-10-26", "steps": 9000},
        )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteRecordStore(root / "athlete.sqlite3")
            ingestion = service.IngestionService(
                athlete_id="athlete-001", store=store, raw_dir=root / "raw"
            )
            ingestion.ingest(data_source.SyncBatch((legacy,)))
            legacy_id = store.list_current("athlete-001", kind="steps")[0].record_id

            result = ingestion.ingest(data_source.SyncBatch((daily,)))

            current = store.list_current("athlete-001", kind="steps")
            self.assertEqual(result.retired, 1)
            self.assertEqual(len(current), 1)
            self.assertEqual(
                result.changed_record_ids,
                tuple(sorted((legacy_id, current[0].record_id))),
            )
            self.assertEqual(result.affected_dates, (date(2026, 10, 26),))
            self.assertEqual(current[0].data["steps"], 9000)

    def test_weight_snapshot_retires_legacy_range_envelope(self) -> None:
        occurred_at = datetime(2026, 10, 22, 0, 0, tzinfo=UTC)
        legacy = data_source.RawSourceRecord(
            source="garmin-cn",
            kind="body-weight",
            source_id="2026-10-20:body-weight:0",
            occurred_at=occurred_at,
            payload={"startDate": "2026-10-20", "endDate": "2026-10-26"},
            canonical_payload={"calendar_date": None, "weight_kg": None},
        )
        weigh_in = data_source.RawSourceRecord(
            source="garmin-cn",
            kind="body-weight",
            source_id="1792660522258",
            occurred_at=occurred_at,
            payload={"calendarDate": "2026-10-22", "weight": 70000},
            canonical_payload={"calendar_date": "2026-10-22", "weight_kg": 70.0},
        )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteRecordStore(root / "athlete.sqlite3")
            ingestion = service.IngestionService(
                athlete_id="athlete-001", store=store, raw_dir=root / "raw"
            )
            ingestion.ingest(data_source.SyncBatch((legacy,)))

            result = ingestion.ingest(data_source.SyncBatch((weigh_in,)))

            current = store.list_current("athlete-001", kind="body-weight")
            self.assertEqual(result.retired, 1)
            self.assertEqual(len(current), 1)
            self.assertEqual(current[0].data["weight_kg"], 70.0)


if __name__ == "__main__":
    unittest.main()
