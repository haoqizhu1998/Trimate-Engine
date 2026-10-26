from __future__ import annotations

import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from athlete_growth.adapters.storage import ProfileRegistry, SQLiteRecordStore, VaultLayout
from athlete_growth.interfaces.cli import main as cli_main

FIXTURES = Path(__file__).parents[1] / "fixtures"


class CLIWorkflowTests(unittest.TestCase):
    def run_command(self, arguments: list[str]) -> tuple[int, str]:
        output = StringIO()
        with redirect_stdout(output):
            exit_code = cli_main.main(arguments)
        return exit_code, output.getvalue()

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
