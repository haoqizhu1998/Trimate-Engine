from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from athlete_growth.adapters.storage import ProfileRegistry, SQLiteRecordStore, VaultLayout
from athlete_growth.domain.records import CanonicalRecord
from athlete_growth.interfaces.cli import main as cli_main
from athlete_growth.interfaces.mcp import server


class CapabilityMCPTests(unittest.TestCase):
    def _call(self, home: Path, name: str, arguments: dict[str, object] | None = None):
        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            },
            home,
        )
        assert response is not None
        return response["result"]

    def _init(self, home: Path) -> None:
        with redirect_stdout(StringIO()):
            result = cli_main.main(
                [
                    "init",
                    "--name",
                    "Synthetic Athlete",
                    "--timezone",
                    "Asia/Shanghai",
                    "--data-home",
                    str(home),
                ]
            )
        self.assertEqual(result, 0)

    def test_onboarding_and_read_capabilities(self) -> None:
        with TemporaryDirectory() as temporary:
            home = Path(temporary) / "vault"
            catalog = self._call(home, "get_capability_catalog")["structuredContent"]
            devices = next(
                item for item in catalog["capabilities"]
                if item["domain"] == "device_connections"
            )
            self.assertEqual({item["id"] for item in devices["sources"]}, {"garmin-cn", "coros"})
            self.assertEqual(
                next(item for item in devices["sources"] if item["id"] == "coros")[
                    "product_ingestion"
                ],
                "experimental_poll_requires_structured_mcp",
            )
            before = self._call(home, "get_onboarding_status")
            self.assertFalse(before["structuredContent"]["profile_ready"])
            self._init(home)
            after = self._call(home, "get_onboarding_status")
            self.assertTrue(after["structuredContent"]["profile_ready"])
            profile = self._call(home, "get_profile_summary")["structuredContent"]
            self.assertEqual(profile["timezone"], "Asia/Shanghai")
            self.assertNotIn("vault", str(profile))
            activities = self._call(home, "list_activities")["structuredContent"]
            self.assertEqual(activities["activities"], [])
            quality = self._call(home, "get_data_quality")["structuredContent"]
            self.assertEqual(quality["record_count"], 0)
            self.assertEqual(
                self._call(home, "list_training_plans")["structuredContent"]["plans"], []
            )

    def test_check_in_requires_exact_preview_and_is_idempotent(self) -> None:
        with TemporaryDirectory() as temporary:
            home = Path(temporary) / "vault"
            self._init(home)
            values = {
                "occurred_at": datetime.now(UTC).isoformat(),
                "fatigue": 3,
                "soreness": 2,
                "pain": 0,
                "mood": 7,
                "motivation": 8,
                "illness": False,
            }
            preview = self._call(home, "preview_check_in", values)["structuredContent"]
            self.assertTrue(preview["requires_confirmation"])
            changed = self._call(
                home,
                "confirm_check_in",
                {**values, "pain": 5, "preview_hash": preview["preview_hash"]},
            )
            self.assertTrue(changed["isError"])
            self.assertEqual(
                changed["structuredContent"]["error"], "check-in changed; preview it again"
            )
            first = self._call(
                home, "confirm_check_in", {**values, "preview_hash": preview["preview_hash"]}
            )["structuredContent"]
            second = self._call(
                home, "confirm_check_in", {**values, "preview_hash": preview["preview_hash"]}
            )["structuredContent"]
            self.assertEqual(first["record_id"], second["record_id"])
            history = self._call(home, "get_recent_check_ins", {"days": 90})[
                "structuredContent"
            ]
            self.assertEqual(history["status"], "observed")
            self.assertEqual(len(history["check_ins"]), 1)
            self.assertEqual(history["check_ins"][0]["responses"]["fatigue"], 3)

    def test_activity_analysis_reads_current_private_record(self) -> None:
        with TemporaryDirectory() as temporary:
            home = Path(temporary) / "vault"
            self._init(home)
            athlete_id = ProfileRegistry(home / "config.toml").default_profile_id()
            assert athlete_id is not None
            store = SQLiteRecordStore(VaultLayout(home, athlete_id).database_file)
            record = CanonicalRecord.create(
                athlete_id=athlete_id,
                kind="activity",
                occurred_at=datetime.now(UTC),
                data={
                    "sport": "running",
                    "duration_min": 50,
                    "distance_m": 10000,
                    "laps": [{"duration_min": 25, "distance_m": 5000}],
                    "secret_route": "never exposed",
                },
            )
            store.save(record)
            analysis = self._call(home, "analyze_session", {"record_id": record.record_id})[
                "structuredContent"
            ]
            self.assertEqual(analysis["summary"], {"pace_min_per_km": 5.0})
            self.assertNotIn("secret_route", str(analysis))
            baseline = self._call(home, "get_sport_baseline", {"sport": "running"})[
                "structuredContent"
            ]
            self.assertEqual(baseline["status"], "insufficient")
            load = self._call(home, "get_training_load_context", {"sport": "running"})[
                "structuredContent"
            ]
            self.assertEqual(load["sports"][0]["sessions_7d"], 1)
            self.assertIsNone(load["sports"][0]["load_7d"])
            self.assertNotIn("secret_route", str(load))
            missing = self._call(home, "get_activity_detail", {"record_id": "missing"})
            self.assertTrue(missing["isError"])
            self.assertEqual(
                missing["structuredContent"]["error"], "current activity record not found"
            )

    def test_check_in_preview_is_bound_to_one_profile(self) -> None:
        with TemporaryDirectory() as temporary:
            first_home = Path(temporary) / "first"
            second_home = Path(temporary) / "second"
            self._init(first_home)
            self._init(second_home)
            values = {
                "occurred_at": "2026-10-27T08:00:00+08:00",
                "fatigue": 3,
                "soreness": 2,
                "pain": 0,
                "mood": 7,
                "motivation": 8,
                "illness": False,
            }
            preview = self._call(first_home, "preview_check_in", values)["structuredContent"]
            cross_profile = self._call(
                second_home,
                "confirm_check_in",
                {**values, "preview_hash": preview["preview_hash"]},
            )
            self.assertTrue(cross_profile["isError"])
            self.assertEqual(
                cross_profile["structuredContent"]["error"],
                "check-in changed; preview it again",
            )

    def test_preview_only_guidance_and_bounded_inputs(self) -> None:
        with TemporaryDirectory() as temporary:
            home = Path(temporary) / "vault"
            recovery = self._call(
                home,
                "assess_return_to_training",
                {
                    "body_area": "knee",
                    "started_on": "2026-10-20",
                    "pain": 8,
                    "swelling": True,
                    "normal_daily_function": False,
                    "stable_days": 0,
                },
            )["structuredContent"]
            self.assertTrue(recovery["requires_professional_review"])
            fueling = self._call(
                home,
                "preview_fueling_plan",
                {"weight_kg": 70, "duration_min": 90, "intensity": "moderate", "is_race": False},
            )["structuredContent"]
            self.assertFalse(fueling["saved"])
            logistics = self._call(
                home,
                "preview_fueling_logistics",
                {
                    "duration_min": 120,
                    "declared_tested_carbs_g_per_hour": 60,
                    "carbs_per_serving_g": 30,
                    "available_servings": 4,
                    "opportunity_times_min": [30, 60, 90, 120],
                },
            )["structuredContent"]
            self.assertTrue(logistics["meets_declared_target"])
            strategy = self._call(
                home,
                "preview_race_strategy",
                {
                    "sport": "running",
                    "event_name": "Synthetic 10K",
                    "event_date": "2026-12-01",
                    "distance_km": 10,
                    "target_finish_min": 55,
                },
            )["structuredContent"]
            self.assertFalse(strategy["saved"])
            self.assertIn("unverified", " ".join(strategy["strategy"]["data_gaps"]))
            unsupported = self._call(
                home,
                "preview_race_strategy",
                {
                    "sport": "triathlon",
                    "event_name": "Synthetic Triathlon",
                    "event_date": "2026-12-01",
                    "distance_km": 51.5,
                    "target_finish_min": 180,
                },
            )
            self.assertTrue(unsupported["isError"])
            self.assertIn("running only", unsupported["structuredContent"]["error"])
            segments = self._call(
                home,
                "preview_race_segments",
                {
                    "sport": "cycling",
                    "event_name": "Synthetic Ride",
                    "event_date": "2026-12-01",
                    "segments": [
                        {"sport": "cycling", "distance_km": 40, "target_duration_min": 80}
                    ],
                },
            )["structuredContent"]
            self.assertEqual(segments["segments"][0]["derived_target"], {
                "speed_km_per_hour": 30.0
            })
            insight = self._call(
                home,
                "analyze_response_samples",
                {
                    "stimulus_name": "weekly minutes",
                    "outcome_name": "10K time",
                    "samples": [[100, 60], [120, 58], [140, 56]],
                },
            )["structuredContent"]
            self.assertEqual(insight["sample_size"], 3)
            rejected = self._call(home, "list_activities", {"limit": 1000})
            self.assertTrue(rejected["isError"])
            blocked_path = self._call(
                home, "preview_race_strategy", {"event_file": "/private/secret"}
            )
            self.assertTrue(blocked_path["isError"])

    def test_sync_write_tool_calls_existing_bounded_host_operation(self) -> None:
        with patch.object(
            server.capability_mcp.operations, "run_automation_once", return_value={"processed": 1}
        ) as run:
            result = self._call(Path("/private/synthetic"), "run_sync_now")
        self.assertEqual(result["structuredContent"], {"processed": 1})
        self.assertTrue(run.call_args.kwargs["force"])


if __name__ == "__main__":
    unittest.main()
