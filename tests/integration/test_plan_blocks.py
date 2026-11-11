from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from athlete_growth.adapters.storage import (
    ProfileRegistry,
    SQLitePlanningDraftRepository,
    SQLiteRecordStore,
    VaultLayout,
)
from athlete_growth.application.plan.workflow import PlanningWorkflow
from athlete_growth.domain.records import CanonicalRecord
from athlete_growth.domain.training import TrainingConstraints
from athlete_growth.interfaces.cli import main as cli_main
from athlete_growth.interfaces.mcp import server


class PlanBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        database = Path(self.temporary.name) / "synthetic.sqlite3"
        self.records = SQLiteRecordStore(database)
        self.drafts = SQLitePlanningDraftRepository(database, self.records)
        self.now = datetime(2026, 10, 27, 9, tzinfo=UTC)
        self.workflow = PlanningWorkflow("athlete-1", self.records, self.drafts, clock=self.now)
        self.records.save(
            CanonicalRecord.create(
                athlete_id="athlete-1",
                kind="check-in",
                occurred_at=self.now,
                data={"pain": 0, "fatigue": 2, "soreness": 2, "illness": False},
            )
        )
        for sport, minutes in (("running", 60), ("cycling", 90)):
            self.records.save(
                CanonicalRecord.create(
                    athlete_id="athlete-1",
                    kind="activity",
                    occurred_at=self.now - timedelta(days=1),
                    data={"sport": sport, "duration_min": minutes},
                )
            )
        proposal = self.workflow.propose_goal(idea="Build endurance", goal_type="race")
        self.goal_id = str(
            self.workflow.confirm_goal(str(proposal["proposal_id"]), int(proposal["revision"]))[
                "goal"
            ]["goal_id"]
        )

    def _constraints(self) -> TrainingConstraints:
        return TrainingConstraints(
            {weekday: 50 for weekday in range(7)},
            frozenset(),
            180,
            ("running", "cycling"),
        )

    def test_explicit_rotation_keeps_future_weeks_as_forecast(self) -> None:
        preview = self.workflow.preview_plan_blocks(
            goal_id=self.goal_id,
            start_date=date(2026, 10, 27),
            constraints=self._constraints(),
            weeks=5,
            focus_sequence=("running", "cycling"),
        )
        self.assertEqual(preview["revision"], 2)
        cycle = preview["block_cycle"]
        self.assertEqual(preview["planning_context"]["constraints"]["focus_sport"], "running")
        self.assertGreater(preview["summary"]["key_sessions"], 0)
        self.assertTrue(
            all(
                session["sport"] == "running"
                for session in preview["plan"]["sessions"]
                if session["session_type"] in {"quality", "long"}
            )
        )
        self.assertEqual(
            [week["focus_sport"] for week in cycle["forecast"]],
            ["running", "cycling", "running", "cycling", "running"],
        )
        self.assertTrue(all(week["status"] == "forecast_only" for week in cycle["forecast"][1:]))
        first_ceiling = cycle["forecast"][0]["weekly_ceiling_min"]
        self.assertEqual(cycle["forecast"][1]["weekly_ceiling_min"], first_ceiling)
        self.assertEqual(cycle["forecast"][2]["weekly_ceiling_min"], first_ceiling)
        self.assertTrue(cycle["forecast"][3]["phase_reduction_is_heuristic"])
        self.assertIn("No volume increase is forecast", cycle["progression_interpretation"])
        self.assertEqual(self.records.list_current("athlete-1", kind="training-plan"), [])
        first = self.workflow.confirm_plan(str(preview["preview_id"]), 2)["plan"]
        self.assertEqual(first["block_week_index"], 0)
        self.assertEqual(len(self.records.list_current("athlete-1", kind="training-plan")), 1)
        with self.assertRaisesRegex(ValueError, "forecast week boundary"):
            self.workflow.preview_plan_update(
                plan_id=str(first["plan_id"]), start_date=date(2026, 10, 30)
            )
        update = self.workflow.preview_plan_update(plan_id=str(first["plan_id"]))
        self.assertEqual(update["block_week_index"], 1)
        self.assertEqual(update["planning_context"]["constraints"]["focus_sport"], "cycling")
        self.assertEqual(update["summary"]["key_sessions"], 0)
        self.assertEqual(update["planning_context"]["baseline_mode"], "restart_or_unknown")
        self.assertTrue(
            all(
                session["sport"] == "cycling"
                for session in update["plan"]["sessions"]
                if session["session_type"] in {"quality", "long"}
            )
        )
        self.assertEqual(
            update["replanning_context"]["block_focus_change"], {"from": "running", "to": "cycling"}
        )
        second = self.workflow.confirm_plan(str(update["preview_id"]), 2)["plan"]
        third = self.workflow.preview_plan_update(plan_id=str(second["plan_id"]))
        self.assertEqual(third["planning_context"]["constraints"]["focus_sport"], "running")
        self.assertEqual(third["block_week_index"], 2)

    def test_established_baseline_forecasts_conditional_growth(self) -> None:
        from athlete_growth.application.plan.blocks import add_progression_envelope

        start = date(2026, 10, 27)
        raw = {
            "base_constraints": {"max_weekly_minutes": 300},
            "forecast": [
                {
                    "start_date": (start + timedelta(days=7 * index)).isoformat(),
                    "end_date": (start + timedelta(days=7 * index + 6)).isoformat(),
                    "status": "current_week_draft" if index == 0 else "forecast_only",
                }
                for index in range(5)
            ],
        }
        cycle = add_progression_envelope(
            raw, observed_first_week_minutes=180,
            goal_type="sport", goal_deadline=None, progression_eligible=True,
        )
        weeks = cycle["forecast"]
        self.assertGreater(weeks[1]["weekly_ceiling_min"], 180)
        self.assertGreater(weeks[2]["weekly_ceiling_min"], weeks[1]["weekly_ceiling_min"])
        self.assertLess(weeks[3]["weekly_ceiling_min"], weeks[2]["weekly_ceiling_min"])
        self.assertTrue(weeks[1]["conditional_growth_is_heuristic"])
        self.assertTrue(weeks[1]["requires_fresh_evidence_and_confirmation"])

    def test_requires_confirmed_preference_or_explicit_sequence(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit sequence or confirmed preference"):
            self.workflow.preview_plan_blocks(
                goal_id=self.goal_id,
                start_date=date(2026, 10, 27),
                constraints=self._constraints(),
                weeks=3,
            )
        with self.assertRaisesRegex(ValueError, "between 2 and 12"):
            self.workflow.preview_plan_blocks(
                goal_id=self.goal_id,
                start_date=date(2026, 10, 27),
                constraints=self._constraints(),
                weeks=13,
                focus_sequence=("running", "cycling"),
            )

    def test_profile_rotation_rechecks_preference_version(self) -> None:
        first_preference = CanonicalRecord.create(
            athlete_id="athlete-1",
            kind="training-preference",
            occurred_at=self.now,
            data={
                "schema_version": 1,
                "preferences": {
                    "key_session_pattern": "focus_blocks",
                    "aerobic_mixing": True,
                },
                "source": "user_confirmed",
            },
        )
        self.records.replace_singleton("athlete-1", "training-preference", None, first_preference)
        preview = self.workflow.preview_plan_blocks(
            goal_id=self.goal_id,
            start_date=date(2026, 10, 27),
            constraints=self._constraints(),
            weeks=3,
        )
        self.assertEqual(preview["block_cycle"]["focus_source"], "confirmed_profile")
        first = self.workflow.confirm_plan(str(preview["preview_id"]), 2)["plan"]
        replacement = CanonicalRecord.create(
            athlete_id="athlete-1",
            kind="training-preference",
            occurred_at=self.now,
            data={
                "schema_version": 1,
                "preferences": {
                    "key_session_pattern": "mixed",
                    "aerobic_mixing": True,
                },
                "source": "user_confirmed",
            },
            supersedes_id=first_preference.record_id,
        )
        self.records.replace_singleton(
            "athlete-1", "training-preference", first_preference.record_id, replacement
        )
        review = self.workflow.review_plan(str(first["plan_id"]))
        self.assertIn("confirmed training preferences changed", review["reasons"])
        with self.assertRaisesRegex(ValueError, "block preference changed"):
            self.workflow.preview_plan_update(plan_id=str(first["plan_id"]))

    def test_mcp_exposes_block_preview_and_confirmation(self) -> None:
        with TemporaryDirectory() as temporary:
            home = Path(temporary) / "vault"
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    cli_main.main(
                        [
                            "init",
                            "--name",
                            "Synthetic Athlete",
                            "--timezone",
                            "UTC",
                            "--data-home",
                            str(home),
                        ]
                    ),
                    0,
                )
            athlete_id = ProfileRegistry(home / "config.toml").default_profile_id()
            assert athlete_id is not None
            store = SQLiteRecordStore(VaultLayout(home, athlete_id).database_file)
            proposal = server.call_tool(
                "propose_goal",
                {
                    "idea": "Build endurance",
                    "goal_type": "race",
                },
                home,
            )
            goal = server.call_tool(
                "confirm_goal",
                {
                    "proposal_id": proposal["proposal_id"],
                    "revision": proposal["revision"],
                },
                home,
            )["goal"]
            today = datetime.now(UTC).date()
            preview = server.call_tool(
                "preview_plan_blocks",
                {
                    "goal_id": goal["goal_id"],
                    "start_date": today.isoformat(),
                    "available_minutes_by_weekday": {str(day): 40 for day in range(7)},
                    "max_weekly_minutes": 120,
                    "sports": ["running", "cycling"],
                    "weeks": 3,
                    "focus_sequence": ["cycling", "running"],
                },
                home,
            )
            self.assertEqual(preview["block_cycle"]["forecast"][1]["focus_sport"], "running")
            self.assertEqual(store.list_current(athlete_id, kind="training-plan"), [])
            saved = server.call_tool(
                "confirm_plan",
                {
                    "preview_id": preview["preview_id"],
                    "revision": preview["revision"],
                },
                home,
            )
            self.assertEqual(saved["plan"]["block_week_index"], 0)
            self.assertEqual(len(store.list_current(athlete_id, kind="training-plan")), 1)


if __name__ == "__main__":
    unittest.main()
