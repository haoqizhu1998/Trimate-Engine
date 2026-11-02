from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from athlete_growth.adapters.storage import SQLitePlanningDraftRepository, SQLiteRecordStore
from athlete_growth.application.plan.workflow import PlanningWorkflow
from athlete_growth.domain.records import CanonicalRecord
from athlete_growth.domain.training import TrainingConstraints


class PlanAdjustmentTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = Path(temporary.name) / "synthetic.sqlite3"
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
        for sport, minutes in (("running", 65), ("cycling", 75)):
            self.records.save(
                CanonicalRecord.create(
                    athlete_id="athlete-1",
                    kind="activity",
                    occurred_at=self.now - timedelta(days=1),
                    data={"sport": sport, "duration_min": minutes},
                )
            )
        goal = self.workflow.propose_goal(idea="Build endurance", goal_type="race")
        goal_id = self.workflow.confirm_goal(str(goal["proposal_id"]), 1)["goal"]["goal_id"]
        constraints = TrainingConstraints(
            {1: 50, 3: 50, 4: 45, 5: 60},
            frozenset(),
            180,
            ("running", "cycling"),
            preferred_quality_weekday=1,
            preferred_long_weekday=5,
            focus_sport="running",
        )
        preview = self.workflow.preview_plan(
            goal_id=goal_id, start_date=date(2026, 10, 27), constraints=constraints
        )
        self.plan = self.workflow.confirm_plan(str(preview["preview_id"]), 1)["plan"]

    def test_shortening_and_skip_are_previewed_without_makeup(self) -> None:
        key = next(item for item in self.plan["sessions"] if item["session_type"] == "quality")
        preview = self.workflow.preview_plan_adjustment(
            plan_id=str(self.plan["plan_id"]),
            session_id=str(key["session_id"]),
            action="shorten",
            new_duration_min=15,
            reason="I feel tired and want a shorter session",
        )
        self.assertEqual(preview["adjustment_context"]["source"], "user_requested_schedule_change")
        self.assertFalse(preview["adjustment_context"]["makeup_session_added"])
        self.assertEqual(len(self.records.list_current("athlete-1", kind="training-plan")), 1)
        revised = next(
            item for item in preview["plan"]["sessions"] if item["session_id"] == key["session_id"]
        )
        self.assertEqual((revised["duration_min"], revised["intensity"]), (15, "easy"))
        approved = self.workflow.confirm_plan(str(preview["preview_id"]), 1)["plan"]
        self.assertEqual(approved["previous_plan_id"], self.plan["plan_id"])
        self.assertEqual(len(self.records.list_current("athlete-1", kind="training-plan")), 2)
        with self.assertRaisesRegex(ValueError, "plan version changed"):
            self.workflow.preview_plan_adjustment(
                plan_id=str(self.plan["plan_id"]),
                session_id=str(key["session_id"]),
                action="skip",
                reason="No time",
            )
        remaining = next(
            item for item in approved["sessions"] if item["session_date"] == "2026-10-29"
        )
        skipped = self.workflow.preview_plan_adjustment(
            plan_id=str(approved["plan_id"]),
            session_id=str(remaining["session_id"]),
            action="skip",
            reason="No time",
        )
        self.assertNotIn(
            remaining["session_id"], [s["session_id"] for s in skipped["plan"]["sessions"]]
        )
        self.assertLess(skipped["summary"]["total_minutes"], preview["summary"]["total_minutes"])

    def test_substitute_and_move_respect_sport_exposure_and_week(self) -> None:
        key = next(item for item in self.plan["sessions"] if item["session_type"] == "long")
        with self.assertRaisesRegex(ValueError, "conservative cap"):
            self.workflow.preview_plan_adjustment(
                plan_id=str(self.plan["plan_id"]),
                session_id=str(key["session_id"]),
                action="substitute",
                new_sport="cycling",
                new_duration_min=100,
                reason="Prefer cycling",
            )
        substitute = self.workflow.preview_plan_adjustment(
            plan_id=str(self.plan["plan_id"]),
            session_id=str(key["session_id"]),
            action="substitute",
            new_sport="cycling",
            new_duration_min=min(30, key["duration_min"]),
            reason="Prefer cycling",
        )
        changed = next(
            item
            for item in substitute["plan"]["sessions"]
            if item["session_id"] == key["session_id"]
        )
        self.assertEqual((changed["sport"], changed["intensity"]), ("cycling", "easy"))
        with self.assertRaisesRegex(ValueError, "current plan week"):
            self.workflow.preview_plan_adjustment(
                plan_id=str(self.plan["plan_id"]),
                session_id=str(key["session_id"]),
                action="move",
                new_date=date(2026, 11, 5),
                reason="Weekend unavailable",
            )

    def test_new_activity_is_only_a_possible_deviation_and_stales_preview(self) -> None:
        session = next(
            item for item in self.plan["sessions"] if item["session_date"] == "2026-10-29"
        )
        preview = self.workflow.preview_plan_adjustment(
            plan_id=str(self.plan["plan_id"]),
            session_id=str(session["session_id"]),
            action="skip",
            reason="Work conflict",
        )
        self.records.save(
            CanonicalRecord.create(
                athlete_id="athlete-1",
                kind="activity",
                occurred_at=self.now,
                data={"sport": "cycling", "duration_min": 35},
            )
        )
        review = self.workflow.review_plan(str(self.plan["plan_id"]))
        self.assertTrue(review["review_required"])
        self.assertEqual(len(review["possible_activity_changes"]), 1)
        self.assertIn(
            "possible deviation", review["possible_activity_changes"][0]["interpretation"]
        )
        self.assertFalse(review["automatic_change_applied"])
        with self.assertRaisesRegex(ValueError, "source data changed"):
            self.workflow.confirm_plan(str(preview["preview_id"]), 1)


if __name__ == "__main__":
    unittest.main()
