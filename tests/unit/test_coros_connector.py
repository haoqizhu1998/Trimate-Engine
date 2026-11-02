from __future__ import annotations

import unittest
from datetime import date
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from athlete_growth.adapters.coros import CorosConnector, install_official_helper
from athlete_growth.adapters.storage import SQLiteRecordStore
from athlete_growth.application.ingest import IngestionService
from athlete_growth.ports.data_source import RawSourceRecord, SyncBatch, SyncWindow


class FakeGateway:
    def __init__(self, *, prose: bool = False, full: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.prose = prose
        self.full = full

    def call(self, tool: str, arguments: dict[str, object]) -> object:
        self.calls.append((tool, arguments))
        if tool == "querySportRecords":
            if self.prose:
                return {"content": [{"type": "text", "text": "Sport Records (1 records)"}]}
            if self.full:
                return {"structuredContent": {"records": [
                    {"labelId": str(index), "sportType": 1}
                    for index in range(100)
                ]}}
            return {"structuredContent": {"records": [{
                "labelId": "activity-1", "sportType": 1,
                "startTime": "2026-10-20T07:00:00+08:00", "sportName": "running",
                "distanceKm": 10.0,
            }]}}
        if tool == "getActivityDetail":
            return {"structuredContent": {
                "durationSeconds": 3600, "avgHr": 145, "distanceMeters": 10000,
            }}
        if tool == "queryActivityLapData":
            return {"content": [{"type": "text", "text": (
                '{"lapGroups":[{"laps":[{"lapIndex":1,"avgHr":145}]}]}'
            )}]}
        return {"structuredContent": {"user": "synthetic"}}


class CorosConnectorTests(unittest.TestCase):
    def test_official_helper_installs_only_in_private_data_home(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = b"#!/usr/bin/env python3\n# login-start call-tool\n"
            with patch(
                "athlete_growth.adapters.coros.connector.urllib.request.urlopen",
                return_value=BytesIO(source),
            ):
                path = install_official_helper(root)
            self.assertEqual(path, root / "integrations" / "coros_mcp_login.py")
            self.assertEqual(path.read_bytes(), source)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_sync_fetches_detail_and_laps_with_stable_source_id(self) -> None:
        with TemporaryDirectory() as temporary:
            gateway = FakeGateway()
            connector = CorosConnector(home=Path(temporary), gateway=gateway)
            batch = connector.sync(SyncWindow(date(2026, 10, 20), date(2026, 10, 20)), None)
            self.assertEqual(len(batch.records), 1)
            activity = batch.records[0]
            self.assertEqual(activity.source_id, "activity-1")
            self.assertEqual(activity.canonical_payload["duration_min"], 60)
            self.assertEqual(activity.canonical_payload["distance_m"], 10000)
            self.assertEqual(activity.canonical_payload["lap_count"], 1)
            self.assertEqual(activity.canonical_payload["detail_status"], "partial")
            self.assertEqual([call[0] for call in gateway.calls], [
                "querySportRecords", "getActivityDetail", "queryActivityLapData",
            ])
            self.assertEqual(connector.capabilities().source, "coros")

    def test_prose_response_fails_without_creating_guesswork_records(self) -> None:
        with TemporaryDirectory() as temporary:
            connector = CorosConnector(home=Path(temporary), gateway=FakeGateway(prose=True))
            with self.assertRaisesRegex(ValueError, "machine-readable activity IDs"):
                connector.sync(SyncWindow(date(2026, 10, 20), date(2026, 10, 20)), None)

    def test_full_list_fails_before_detail_calls(self) -> None:
        with TemporaryDirectory() as temporary:
            gateway = FakeGateway(full=True)
            connector = CorosConnector(home=Path(temporary), gateway=gateway)
            with self.assertRaisesRegex(ValueError, "reached its limit"):
                connector.sync(SyncWindow(date(2026, 10, 20), date(2026, 10, 20)), None)
            self.assertEqual(len(gateway.calls), 1)

    def test_repeated_coros_and_exact_garmin_duplicate_count_once(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            batch = CorosConnector(home=root, gateway=FakeGateway()).sync(
                SyncWindow(date(2026, 10, 20), date(2026, 10, 20)), None
            )
            coros = batch.records[0]
            garmin = RawSourceRecord(
                source="garmin-cn", kind="activity", source_id="garmin-1",
                occurred_at=coros.occurred_at,
                payload={"source": "synthetic Garmin"},
                canonical_payload={
                    "sport": "running", "duration_min": 60,
                    "distance_m": 10000, "detail_status": "complete",
                },
            )
            store = SQLiteRecordStore(root / "athlete.sqlite3")
            ingest = IngestionService(
                athlete_id="synthetic", store=store, raw_dir=root / "raw"
            )
            self.assertEqual(ingest.ingest(SyncBatch((garmin,))).inserted, 1)
            first = ingest.ingest(batch)
            second = ingest.ingest(batch)
            self.assertEqual(first.duplicates, 1)
            self.assertEqual(second.duplicates, 1)
            self.assertEqual(len(store.list_current("synthetic", kind="activity")), 1)
            self.assertTrue(list((root / "raw" / "coros").rglob("*.json")))


if __name__ == "__main__":
    unittest.main()
