from __future__ import annotations

import io
import stat
import unittest
import zipfile
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zoneinfo import ZoneInfo

from athlete_growth.adapters.garmin_global.connector import (
    GarminGlobalConnector,
    PersonalApiGateway,
)
from athlete_growth.ports.data_source import SyncWindow


class FakeGateway:
    def __init__(self, *, valid: bool = True) -> None:
        self.valid = valid
        self.keys: list[str] = []

    def verify_key(self, key: str) -> bool:
        self.keys.append(key)
        return self.valid

    def fetch(self, start: date, end: date) -> list[dict[str, object]]:
        return [
            {
                "kind": "sleep",
                "source_id": start.isoformat(),
                "occurred_at": f"{start.isoformat()}T00:00:00+08:00",
                "payload": {
                    "calendarDate": start.isoformat(),
                    "dailySleepDTO": {"sleepTimeSeconds": 28800},
                },
            }
        ]

    def healthcheck(self) -> bool:
        return True


class FakePersonalClient:
    def __init__(self) -> None:
        self.activity_offsets: list[int] = []
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zip_file:
            zip_file.writestr("activity.fit", b"invalid synthetic FIT")
        self.original = archive.getvalue()

    def __enter__(self) -> FakePersonalClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def search_activities(self, **kwargs: object) -> list[dict[str, object]]:
        self.activity_offsets.append(int(kwargs["start"]))
        return [{
            "activityId": 42,
            "startTimeLocal": "2026-11-04 07:00:00",
            "activityName": "Easy Run",
            "activityType": {"typeKey": "running"},
            "duration": 1800,
            "distance": 5000,
        }]

    def get_activity(self, activity_id: str) -> dict[str, object]:
        assert activity_id == "42"
        return {"activityId": 42, "averageHR": 140}

    def get_activity_workouts(self, activity_id: str) -> None:
        return None

    def download_activity(self, activity_id: str) -> bytes:
        return self.original

    def get_daily_summaries(self, start: str, end: str) -> dict[str, object]:
        return {"data": [{"calendarDate": start, "totalSteps": 9000}]}

    def get_daily_steps(self, start: str, end: str) -> list[dict[str, object]]:
        return [{"calendarDate": start, "totalSteps": 9000}]

    def get_daily_hrv_summaries(self, start: str, end: str) -> dict[str, object]:
        return {"hrvSummaries": [{"calendarDate": start, "lastNightAvg": 61}]}

    def get_daily_stress(self, start: str, end: str) -> list[dict[str, object]]:
        return [{"calendarDate": start, "overallStressLevel": 25}]

    def get_daily_body_battery(self, start: str, end: str) -> list[dict[str, object]]:
        return [{"calendarDate": start, "highBodyBattery": 88}]

    def get_weight_measurements(self, start: str, end: str) -> dict[str, object]:
        return {"dateWeightList": []}

    def get_daily_sleep(self, day: date) -> dict[str, object]:
        return {"dailySleepDTO": {"sleepTimeSeconds": 28800}}

    def get_daily_heart_rate(self, day: date) -> dict[str, object]:
        return {"values": {"restingHR": 52}}


class GarminGlobalConnectorTests(unittest.TestCase):
    def test_personal_key_is_private_and_region_isolated(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            cn_tokens = home / "tokens" / "garmin-cn-session"
            cn_tokens.mkdir(parents=True)
            (cn_tokens / "garmin_tokens.json").write_text("synthetic-cn")
            gateway = FakeGateway()
            connector = GarminGlobalConnector(
                home=home, timezone_name="Asia/Shanghai", gateway=gateway
            )

            result = connector.connect("  synthetic-global-key  ")
            batch = connector.sync(SyncWindow(date(2026, 11, 4), date(2026, 11, 4)), None)

            self.assertEqual(result["source"], "garmin-global")
            self.assertEqual(gateway.keys, ["synthetic-global-key"])
            self.assertEqual(connector.key_file.read_text(), "synthetic-global-key")
            self.assertEqual(stat.S_IMODE(connector.key_file.stat().st_mode), 0o600)
            self.assertEqual(batch.records[0].source, "garmin-global")
            self.assertEqual(batch.records[0].canonical_payload["sleep_seconds"], 28800)
            self.assertTrue(connector.capabilities().read_only)
            connector.revoke()
            self.assertFalse(connector.key_file.exists())
            self.assertTrue((cn_tokens / "garmin_tokens.json").exists())

    def test_invalid_key_does_not_replace_existing_key(self) -> None:
        with TemporaryDirectory() as directory:
            connector = GarminGlobalConnector(home=Path(directory), gateway=FakeGateway())
            connector.connect("first-key")
            connector._gateway = FakeGateway(valid=False)
            with self.assertRaisesRegex(RuntimeError, "verification failed"):
                connector.connect("bad-key")
            self.assertEqual(connector.key_file.read_text(), "first-key")

    def test_official_client_maps_activity_and_daily_health(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            client = FakePersonalClient()
            gateway = PersonalApiGateway(
                key_file=home / "key",
                bearer_cache_file=home / "token",
                timezone=ZoneInfo("Asia/Shanghai"),
            )
            gateway.key_file.write_text("synthetic-key")
            connector = GarminGlobalConnector(home=home, gateway=gateway)
            with patch.object(PersonalApiGateway, "_client", return_value=client):
                batch = connector.sync(SyncWindow(date(2026, 11, 4), date(2026, 11, 4)), None)

            by_kind = {record.kind: record for record in batch.records}
            self.assertEqual(client.activity_offsets, [0])
            self.assertEqual(by_kind["activity"].source_id, "42")
            self.assertEqual(by_kind["activity"].canonical_payload["duration_min"], 30)
            self.assertEqual(by_kind["activity"].canonical_payload["detail_status"], "partial")
            self.assertEqual(len(by_kind["activity"].artifacts), 1)
            self.assertEqual(by_kind["sleep"].canonical_payload["sleep_seconds"], 28800)
            self.assertEqual(by_kind["hrv"].canonical_payload["last_night_avg_ms"], 61)
            self.assertEqual(by_kind["daily-summary"].canonical_payload["steps"], 9000)


if __name__ == "__main__":
    unittest.main()
