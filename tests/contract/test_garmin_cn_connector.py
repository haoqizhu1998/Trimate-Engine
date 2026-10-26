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
            }
        ]

    def get_user_summary(self, cdate: str) -> dict[str, object]:
        return {"calendarDate": cdate, "totalSteps": 9000}

    def get_steps_data(self, cdate: str) -> list[dict[str, object]]:
        return [{"calendarDate": cdate, "steps": 9000}]

    def get_sleep_data(self, cdate: str) -> dict[str, object]:
        return {"dailySleepDTO": {"calendarDate": cdate, "sleepTimeSeconds": 28000}}

    def get_hrv_data(self, cdate: str) -> dict[str, object]:
        return {"hrvSummary": {"calendarDate": cdate, "weeklyAvg": 52}}

    def get_heart_rates(self, cdate: str) -> dict[str, object]:
        return {"calendarDate": cdate, "restingHeartRate": 52}

    def get_stress_data(self, cdate: str) -> dict[str, object]:
        return {"calendarDate": cdate, "overallStressLevel": 24}

    def get_body_battery(
        self, startdate: str, enddate: str
    ) -> list[dict[str, object]]:
        return [{"date": startdate, "charged": 55, "drained": 40}]

    def get_weigh_ins(self, startdate: str, enddate: str) -> dict[str, object]:
        return {"startDate": startdate, "endDate": enddate, "totalAverage": 70000}


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


if __name__ == "__main__":
    unittest.main()
