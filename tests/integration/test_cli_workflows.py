from __future__ import annotations

import json
import unittest
from contextlib import redirect_stdout
from datetime import UTC, date, datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from athlete_growth.adapters.storage import ProfileRegistry, SQLiteRecordStore, VaultLayout
from athlete_growth.application.ingest.service import IngestionResult, IngestionService
from athlete_growth.domain.records import CanonicalRecord, EvidenceRef
from athlete_growth.interfaces.cli import main as cli_main

FIXTURES = Path(__file__).parents[1] / "fixtures"


class CLIWorkflowTests(unittest.TestCase):
    def run_command(self, arguments: list[str]) -> tuple[int, str]:
        output = StringIO()
        with redirect_stdout(output):
            exit_code = cli_main.main(arguments)
        return exit_code, output.getvalue()

    def test_high_risk_cli_plan_is_not_saved(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            self.run_command([
                "init", "--name", "Synthetic Athlete", "--timezone", "UTC",
                "--data-home", str(home),
            ])
            _, goal_output = self.run_command([
                "goal-add", "--type", "race", "--title", "Synthetic 10K",
                "--priority", "3", "--deadline", "2027-05-01",
                "--data-home", str(home),
            ])
            goal_id = json.loads(goal_output)["goal_id"]
            self.run_command([
                "check-in", "--at", "2027-01-01T08:00:00+00:00",
                "--fatigue", "7", "--soreness", "5", "--pain", "8",
                "--mood", "4", "--motivation", "4", "--rpe", "7",
                "--data-home", str(home),
            ])
            _, output = self.run_command([
                "plan-create", "--goal-id", goal_id,
                "--start-date", "2027-01-01", "--available", "4:60",
                "--max-weekly-minutes", "60", "--sport", "running",
                "--data-home", str(home),
            ])
            self.assertEqual(json.loads(output)["blocked"], True)
            _, plans = self.run_command(["plan-list", "--data-home", str(home)])
            self.assertEqual(json.loads(plans)["plans"], [])

    def test_cli_plan_uses_recent_activity_baseline(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            self.run_command([
                "init", "--name", "Synthetic Athlete", "--timezone", "UTC",
                "--data-home", str(home),
            ])
            athlete_id = ProfileRegistry(home / "config.toml").default_profile_id()
            assert athlete_id is not None
            store = SQLiteRecordStore(VaultLayout(home, athlete_id).database_file)
            store.save(CanonicalRecord.create(
                athlete_id=athlete_id, kind="activity",
                occurred_at=datetime(2026, 10, 25, 8, tzinfo=UTC),
                data={"sport": "road_running", "duration_min": 20},
            ))
            _, goal_output = self.run_command([
                "goal-add", "--type", "sport", "--title", "Consistency",
                "--priority", "3", "--data-home", str(home),
            ])
            _, output = self.run_command([
                "plan-create", "--goal-id", json.loads(goal_output)["goal_id"],
                "--start-date", "2026-10-27", "--available", "1:60,3:60",
                "--max-weekly-minutes", "120", "--sport", "running",
                "--data-home", str(home),
            ])
            plan = json.loads(output)
            self.assertLessEqual(sum(item["duration_min"] for item in plan["sessions"]), 20)

    def test_activity_detail_backfill_is_a_noop_without_garmin_activities(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            code, _ = self.run_command([
                "init", "--name", "Demo Athlete", "--timezone", "Asia/Shanghai",
                "--data-home", str(home),
            ])
            self.assertEqual(code, 0)
            code, output = self.run_command([
                "sync", "--backfill-existing", "--data-home", str(home)
            ])
            self.assertEqual(code, 0)
            result = json.loads(output)
            self.assertEqual(result["chunks"], 0)
            self.assertEqual(result["activity_detail_coverage"]["summary_only"], 0)

    def test_activity_detail_backfill_uses_existing_activity_dates(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            code, _ = self.run_command([
                "init", "--name", "Demo Athlete", "--timezone", "Asia/Shanghai",
                "--data-home", str(home),
            ])
            self.assertEqual(code, 0)
            athlete_id = ProfileRegistry(home / "config.toml").default_profile_id()
            assert athlete_id is not None
            store = SQLiteRecordStore(VaultLayout(home, athlete_id).database_file)
            stored = CanonicalRecord.create(
                athlete_id=athlete_id,
                kind="activity",
                occurred_at=datetime(2022, 3, 4, 23, 0, tzinfo=UTC),
                data={"calendar_date": "2022-03-05", "sport": "running"},
                evidence=(EvidenceRef("garmin-cn", "42", datetime.now(UTC)),),
            )
            store.save(stored)
            with (
                patch("athlete_growth.interfaces.cli.commands.GarminCnConnector"),
                patch.object(
                    IngestionService, "sync_chunked",
                    return_value=IngestionResult(0, 0, 0, 0, 0),
                ) as sync,
            ):
                code, output = self.run_command([
                    "sync", "--backfill-existing", "--data-home", str(home)
                ])
            self.assertEqual(code, 0)
            window = sync.call_args.args[1]
            self.assertEqual((window.start, window.end), (date(2022, 3, 5), date(2022, 3, 5)))
            self.assertEqual(json.loads(output)["activity_detail_coverage"]["summary_only"], 1)

    def test_local_training_workflow_from_import_to_plan_and_export(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            code, _ = self.run_command(
                [
                    "init",
                    "--name",
                    "Demo Athlete",
                    "--timezone",
                    "Asia/Shanghai",
                    "--data-home",
                    str(home),
                ]
            )
            self.assertEqual(code, 0)

            code, imported_output = self.run_command(
                [
                    "import-file",
                    str(FIXTURES / "activities.csv"),
                    "--data-home",
                    str(home),
                ]
            )
            self.assertEqual(json.loads(imported_output)["inserted"], 1)

            code, _ = self.run_command(
                [
                    "check-in",
                    "--at",
                    "2026-10-25T09:00:00+08:00",
                    "--fatigue",
                    "3",
                    "--soreness",
                    "2",
                    "--pain",
                    "0",
                    "--mood",
                    "8",
                    "--motivation",
                    "8",
                    "--rpe",
                    "6",
                    "--data-home",
                    str(home),
                ]
            )
            self.assertEqual(code, 0)

            code, today_output = self.run_command(
                ["today", "--date", "2026-10-25", "--data-home", str(home)]
            )
            today = json.loads(today_output)
            self.assertEqual(today["safety"]["level"], "low")
            self.assertEqual(today["analytics"]["metrics"]["sessions_7d"], 1)
            self.assertTrue(today["evidence_ids"])

            code, goal_output = self.run_command(
                [
                    "goal-add",
                    "--type",
                    "race",
                    "--title",
                    "Demo 10K",
                    "--priority",
                    "5",
                    "--deadline",
                    "2027-01-01",
                    "--data-home",
                    str(home),
                ]
            )
            goal_id = json.loads(goal_output)["goal_id"]
            self.assertTrue(goal_id)

            code, plan_output = self.run_command(
                [
                    "plan-create",
                    "--goal-id",
                    goal_id,
                    "--start-date",
                    "2026-10-26",
                    "--available",
                    "0:45,2:60,5:90",
                    "--unavailable",
                    "2",
                    "--max-weekly-minutes",
                    "120",
                    "--sport",
                    "running",
                    "--data-home",
                    str(home),
                ]
            )
            plan = json.loads(plan_output)
            self.assertLessEqual(sum(session["duration_min"] for session in plan["sessions"]), 120)
            self.assertTrue(
                all(session["session_date"] != "2026-10-28" for session in plan["sessions"])
            )

            archive = root / "profile.zip"
            code, export_output = self.run_command(
                ["export", str(archive), "--data-home", str(home)]
            )
            self.assertEqual(code, 0)
            self.assertTrue(archive.is_file())
            self.assertTrue(json.loads(export_output)["verified"])

            code, nutrition_output = self.run_command(
                [
                    "nutrition",
                    "--weight-kg",
                    "70",
                    "--duration-min",
                    "180",
                    "--intensity",
                    "race",
                    "--race",
                    "--data-home",
                    str(home),
                ]
            )
            self.assertGreaterEqual(json.loads(nutrition_output)["carbohydrate_g_per_hour"][0], 60)
            athlete_id = ProfileRegistry(home / "config.toml").default_profile_id()
            self.assertIsNotNone(athlete_id)
            store = SQLiteRecordStore(VaultLayout(home, str(athlete_id)).database_file)
            nutrition_records = store.list_current(str(athlete_id), kind="nutrition-plan")
            self.assertEqual(len(nutrition_records), 1)


if __name__ == "__main__":
    unittest.main()
