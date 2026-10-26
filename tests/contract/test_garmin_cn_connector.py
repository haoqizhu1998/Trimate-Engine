from __future__ import annotations

import unittest
from collections.abc import Callable
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from athlete_growth.adapters.garmin_cn import connector
from athlete_growth.ports.data_source import SyncWindow


class FakeGarminGateway:
    def __init__(self) -> None:
        self.authenticated_with: tuple[str, str] | None = None

    def authenticate(
        self, email: str, password: str, prompt_mfa: Callable[[], str]
    ) -> dict[str, object]:
        self.authenticated_with = (email, password)
        return {"display_name": "CN Athlete", "region": "cn"}

    def fetch(self, start: date, end: date):
        return [
            {
                "kind": "daily-summary",
                "source_id": "2026-10-26",
                "occurred_at": "2026-10-26T00:00:00+08:00",
                "payload": {"steps": 9000, "resting_hr": 52},
            }
        ]

    def healthcheck(self) -> bool:
        return True


class GarminCnConnectorContractTests(unittest.TestCase):
    def test_declares_poll_delivery_capabilities(self) -> None:
        with TemporaryDirectory() as directory:
            source = connector.GarminCnConnector(
                home=Path(directory), gateway=FakeGarminGateway()
            )

            capabilities = source.capabilities()

            self.assertEqual(capabilities.delivery_mode, "poll")
            self.assertEqual(capabilities.suggested_poll_minutes, 30)

    def test_is_read_only_and_keeps_cn_session_region_isolated(self) -> None:
        connector_type = getattr(connector, "GarminCnConnector", None)
        self.assertIsNotNone(connector_type)

        with TemporaryDirectory() as directory:
            home = Path(directory)
            source = connector_type(home=home, gateway=FakeGarminGateway())
            capabilities = source.capabilities()
            batch = source.sync(SyncWindow(date(2026, 10, 26), date(2026, 10, 26)), None)

            self.assertTrue(capabilities.read_only)
            self.assertFalse(capabilities.supports_write)
            self.assertEqual(source.region, "cn")
            self.assertEqual(source.auth_method, "session")
            self.assertIn("garmin-cn", source.session_token_dir.name)
            self.assertEqual(batch.records[0].source, "garmin-cn")
            self.assertEqual(batch.records[0].payload["steps"], 9000)
            self.assertEqual(batch.next_cursor, "2026-10-26")

    def test_authenticate_delegates_without_persisting_password(self) -> None:
        with TemporaryDirectory() as directory:
            gateway = FakeGarminGateway()
            source = connector.GarminCnConnector(home=Path(directory), gateway=gateway)

            identity = source.authenticate(
                email="athlete@example.com",
                password="one-time-secret",
                prompt_mfa=lambda: "123456",
            )

            self.assertEqual(identity["region"], "cn")
            self.assertEqual(
                gateway.authenticated_with,
                ("athlete@example.com", "one-time-secret"),
            )
            self.assertFalse(hasattr(source, "password"))

    def test_revoke_removes_only_china_region_session(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            source = connector.GarminCnConnector(home=home, gateway=FakeGarminGateway())
            source.session_token_dir.mkdir(parents=True)
            source.session_token_file.write_text("cn-token", encoding="utf-8")
            global_tokens = source.session_token_dir.parent / "garmin-global-session"
            global_tokens.mkdir()
            global_token_file = global_tokens / "garmin_tokens.json"
            global_token_file.write_text("global-token", encoding="utf-8")

            source.revoke()

            self.assertFalse(source.session_token_file.exists())
            self.assertFalse(source.session_token_dir.exists())
            self.assertEqual(global_token_file.read_text(encoding="utf-8"), "global-token")


class FakeGarminClient:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.login_paths: list[str] = []
        self.range_calls: list[tuple[str, str, str]] = []
        self.display_name = "CN Athlete"

    def login(self, tokenstore: str) -> tuple[None, None]:
        self.login_paths.append(tokenstore)
        return None, None

    def get_userprofile_settings(self) -> dict[str, object]:
        return {"displayName": self.display_name}

    def get_activities_by_date(
        self, startdate: str, enddate: str
    ) -> list[dict[str, object]]:
        return [
            {
                "activityId": 42,
                "startTimeLocal": f"{startdate} 07:30:00",
                "activityName": "Easy Run",
                "activityType": {"typeKey": "running"},
                "duration": 1800.0,
                "distance": 5000.0,
                "activityTrainingLoad": 72.5,
                "vigorousIntensityMinutes": 20,
                "averageHR": 145,
                "maxHR": 168,
                "averageSpeed": 2.78,
                "elevationGain": 35.0,
                "calories": 360.0,
                "steps": 5200,
                "deviceId": 12345,
            }
        ]

    def get_daily_summaries(self, start: str, end: str) -> dict[str, object]:
        self.range_calls.append(("daily-summary", start, end))
        return {
            "data": [
                {
                    "calendarDate": start,
                    "movement": {"steps": 9000},
                    "heartRate": {"restingValue": 52},
                    "bodyBattery": {"latestValue": 74},
                    "stress": {"avgLevel": 24},
                }
            ]
        }

    def get_daily_steps(self, start: str, end: str) -> list[dict[str, object]]:
        self.range_calls.append(("steps", start, end))
        return [{"calendarDate": start, "totalSteps": 9000}]

    def get_sleep_daily(self, start: str, end: str) -> list[dict[str, object]]:
        self.range_calls.append(("sleep", start, end))
        return [
            {
                "calendarDate": start,
                "values": {"totalSleepTimeInSeconds": 28000},
            }
        ]

    def get_hrv_data_range(self, start: str, end: str) -> dict[str, object]:
        self.range_calls.append(("hrv", start, end))
        return {"hrvSummaries": [{"calendarDate": start, "weeklyAvg": 52}]}

    def get_daily_heart_rate_stats(
        self, start: str, end: str
    ) -> list[dict[str, object]]:
        self.range_calls.append(("heart-rate", start, end))
        return [{"calendarDate": start, "values": {"restingHR": 52}}]

    def get_daily_stress_stats(self, start: str, end: str) -> list[dict[str, object]]:
        self.range_calls.append(("stress", start, end))
        return [{"calendarDate": start, "values": {"overallStressLevel": 24}}]

    def get_daily_body_battery_stats(
        self, start: str, end: str
    ) -> list[dict[str, object]]:
        self.range_calls.append(("body-battery", start, end))
        return [
            {
                "calendarDate": start,
                "values": {"highBodyBattery": 74, "lowBodyBattery": 19},
            }
        ]

    def get_weight_snapshot(self, start: str, end: str) -> dict[str, object]:
        self.range_calls.append(("body-weight", start, end))
        return {
            "dateWeightList": [
                {
                    "samplePk": 7,
                    "calendarDate": start,
                    "weight": 70000,
                }
            ]
        }


class GarminCnSessionGatewayTests(unittest.TestCase):
    def test_fresh_login_forces_cn_domain_and_saves_to_profile_token_store(self) -> None:
        created: list[FakeGarminClient] = []

        def factory(**kwargs: object) -> FakeGarminClient:
            client = FakeGarminClient(**kwargs)
            created.append(client)
            return client

        with TemporaryDirectory() as directory:
            token_dir = Path(directory) / "tokens" / "garmin-cn-session"
            gateway_type = getattr(connector, "GarminCnSessionGateway", None)
            self.assertIsNotNone(gateway_type)
            gateway = gateway_type(token_dir=token_dir, client_factory=factory)

            identity = gateway.authenticate(
                "athlete@example.com", "secret", prompt_mfa=lambda: "123456"
            )

            self.assertTrue(created[0].kwargs["is_cn"])
            self.assertEqual(created[0].kwargs["email"], "athlete@example.com")
            self.assertEqual(created[0].login_paths, [str(token_dir)])
            self.assertEqual(identity, {"display_name": "CN Athlete", "region": "cn"})

    def test_cached_login_uses_cn_domain_without_requiring_credentials(self) -> None:
        created: list[FakeGarminClient] = []

        def factory(**kwargs: object) -> FakeGarminClient:
            client = FakeGarminClient(**kwargs)
            created.append(client)
            return client

        with TemporaryDirectory() as directory:
            token_dir = Path(directory) / "tokens" / "garmin-cn-session"
            gateway = connector.GarminCnSessionGateway(
                token_dir=token_dir, client_factory=factory
            )

            self.assertTrue(gateway.healthcheck())

            self.assertEqual(created[0].kwargs, {"is_cn": True})
            self.assertEqual(created[0].login_paths, [str(token_dir)])

    def test_fetches_read_only_training_and_body_data_from_cached_session(self) -> None:
        with TemporaryDirectory() as directory:
            gateway = connector.GarminCnSessionGateway(
                token_dir=Path(directory) / "tokens" / "garmin-cn-session",
                client_factory=FakeGarminClient,
            )

            rows = gateway.fetch(date(2026, 10, 26), date(2026, 10, 26))

            self.assertEqual(
                {row["kind"] for row in rows},
                {
                    "activity",
                    "daily-summary",
                    "steps",
                    "sleep",
                    "hrv",
                    "heart-rate",
                    "stress",
                    "body-battery",
                    "body-weight",
                },
            )
            activity = next(row for row in rows if row["kind"] == "activity")
            self.assertEqual(activity["source_id"], "42")
            self.assertEqual(activity["occurred_at"], "2026-10-26T07:30:00+08:00")

    def test_fetch_uses_range_endpoints_once_per_category(self) -> None:
        created: list[FakeGarminClient] = []

        def factory(**kwargs: object) -> FakeGarminClient:
            client = FakeGarminClient(**kwargs)
            created.append(client)
            return client

        with TemporaryDirectory() as directory:
            gateway = connector.GarminCnSessionGateway(
                token_dir=Path(directory) / "tokens" / "garmin-cn-session",
                client_factory=factory,
            )

            gateway.fetch(date(2026, 10, 1), date(2026, 10, 28))

            self.assertEqual(
                created[0].range_calls,
                [
                    ("daily-summary", "2026-10-01", "2026-10-28"),
                    ("steps", "2026-10-01", "2026-10-28"),
                    ("sleep", "2026-10-01", "2026-10-28"),
                    ("hrv", "2026-10-01", "2026-10-28"),
                    ("heart-rate", "2026-10-01", "2026-10-28"),
                    ("stress", "2026-10-01", "2026-10-28"),
                    ("body-battery", "2026-10-01", "2026-10-28"),
                    ("body-weight", "2026-10-01", "2026-10-28"),
                ],
            )

    def test_normalizes_nested_range_metrics(self) -> None:
        cases = {
            "daily-summary": (
                {
                    "calendarDate": "2026-10-26",
                    "movement": {"steps": 9000},
                    "heartRate": {"restingValue": 52},
                    "bodyBattery": {"latestValue": 74},
                    "stress": {"avgLevel": 24},
                },
                {"steps": 9000, "resting_hr": 52, "body_battery": 74},
            ),
            "steps": (
                {"calendarDate": "2026-10-26", "totalSteps": 9000},
                {"steps": 9000},
            ),
            "sleep": (
                {
                    "calendarDate": "2026-10-26",
                    "values": {"totalSleepTimeInSeconds": 28000},
                },
                {"sleep_seconds": 28000},
            ),
            "heart-rate": (
                {"calendarDate": "2026-10-26", "values": {"restingHR": 52}},
                {"resting_hr": 52},
            ),
            "stress": (
                {
                    "calendarDate": "2026-10-26",
                    "values": {"overallStressLevel": 24},
                },
                {"average_stress": 24},
            ),
            "body-battery": (
                {
                    "calendarDate": "2026-10-26",
                    "values": {"highBodyBattery": 74, "lowBodyBattery": 19},
                },
                {"body_battery": 74},
            ),
        }
        for kind, (payload, expected) in cases.items():
            with self.subTest(kind=kind):
                raw = connector.GarminCnConnector._to_raw(
                    {
                        "kind": kind,
                        "source_id": f"2026-10-26:{kind}",
                        "occurred_at": "2026-10-26T00:00:00+08:00",
                        "payload": payload,
                    }
                )
                for key, value in expected.items():
                    self.assertEqual(raw.canonical_payload[key], value)

    def test_connector_keeps_vendor_payload_but_exposes_canonical_activity_metrics(self) -> None:
        with TemporaryDirectory() as directory:
            gateway = connector.GarminCnSessionGateway(
                token_dir=Path(directory) / "tokens" / "garmin-cn-session",
                client_factory=FakeGarminClient,
            )
            source = connector.GarminCnConnector(home=Path(directory), gateway=gateway)

            batch = source.sync(
                SyncWindow(date(2026, 10, 26), date(2026, 10, 26)), None
            )

            activity = next(record for record in batch.records if record.kind == "activity")
            self.assertEqual(activity.payload["activityName"], "Easy Run")
            self.assertEqual(
                activity.canonical_payload,
                {
                    "name": "Easy Run",
                    "sport": "running",
                    "duration_min": 30.0,
                    "distance_m": 5000.0,
                    "training_load": 72.5,
                    "high_intensity_min": 20,
                    "avg_hr": 145,
                    "max_hr": 168,
                    "avg_speed_mps": 2.78,
                    "elevation_gain_m": 35.0,
                    "calories": 360.0,
                    "steps": 5200,
                    "device_id": 12345,
                },
            )

    def test_normalizes_nested_recovery_metrics(self) -> None:
        sleep = connector.GarminCnConnector._to_raw(
            {
                "kind": "sleep",
                "source_id": "2026-10-26:sleep:0",
                "occurred_at": "2026-10-26T00:00:00+08:00",
                "payload": {
                    "dailySleepDTO": {
                        "calendarDate": "2026-10-26",
                        "sleepTimeSeconds": 28000,
                    }
                },
            }
        )
        hrv = connector.GarminCnConnector._to_raw(
            {
                "kind": "hrv",
                "source_id": "2026-10-26:hrv:0",
                "occurred_at": "2026-10-26T00:00:00+08:00",
                "payload": {
                    "hrvSummary": {"calendarDate": "2026-10-26", "lastNightAvg": 61}
                },
            }
        )

        self.assertEqual(sleep.canonical_payload["sleep_seconds"], 28000)
        self.assertEqual(hrv.canonical_payload["hrv"], 61)

    def test_treats_negative_vendor_intensity_sentinel_as_missing(self) -> None:
        activity = connector.GarminCnConnector._to_raw(
            {
                "kind": "activity",
                "source_id": "42",
                "occurred_at": "2026-10-26T07:30:00+08:00",
                "payload": {
                    "activityId": 42,
                    "vigorousIntensityMinutes": -1,
                },
            }
        )

        self.assertIsNone(activity.canonical_payload["high_intensity_min"])


if __name__ == "__main__":
    unittest.main()
