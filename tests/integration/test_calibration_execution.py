from __future__ import annotations

import unittest
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from athlete_growth.adapters.storage import SQLitePlanningDraftRepository, SQLiteRecordStore
from athlete_growth.application.calibration import SportCalibrationService
from athlete_growth.application.plan.blocks import add_progression_envelope
from athlete_growth.application.plan.execution import PlanExecutionService
from athlete_growth.application.plan.workflow import PlanningWorkflow
from athlete_growth.domain.records import CanonicalRecord
from athlete_growth.domain.training import TrainingConstraints


class CalibrationExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = Path(temporary.name) / "athlete.sqlite3"
        self.records = SQLiteRecordStore(database)
        self.drafts = SQLitePlanningDraftRepository(database, self.records)
        self.today = datetime.now(UTC).date()
        self.now = datetime.combine(self.today, time(12), UTC)
        self.workflow = PlanningWorkflow("a1", self.records, self.drafts, clock=self.now)

    def _goal(self) -> str:
        proposal = self.workflow.propose_goal(
            idea="Finish a 10K", goal_type="race", deadline=self.today + timedelta(days=35)
        )
        return str(self.workflow.confirm_goal(
            str(proposal["proposal_id"]), int(proposal["revision"])
        )["goal"]["goal_id"])

    def _plan(self) -> dict:
        goal_id = self._goal()
        preview = self.workflow.preview_plan(
            goal_id=goal_id,
            start_date=self.today,
            constraints=TrainingConstraints(
                {self.today.weekday(): 45}, frozenset(), 45, ("running",)
            ),
        )
        return self.workflow.confirm_plan(str(preview["preview_id"]), 1)["plan"]

    def test_calibration_requires_confirmation_and_expires(self) -> None:
        service = SportCalibrationService("a1", self.records, today=self.today)
        activity = CanonicalRecord.create(
            athlete_id="a1", kind="activity", occurred_at=self.now,
            data={"sport": "running", "duration_min": 30, "distance_m": 6000},
        )
        self.records.save(activity)
        preview = service.preview(
            "running", 5.0, self.today, "field_test", activity.record_id
        )
        self.assertEqual(service.get("running")["status"], "missing")
        with self.assertRaisesRegex(ValueError, "preview it again"):
            service.confirm(
                "running", 5.1, self.today, "field_test", str(preview["preview_hash"]),
                activity.record_id,
            )
        confirmed = service.confirm(
            "running", 5.0, self.today, "field_test", str(preview["preview_hash"]),
            activity.record_id,
        )
        self.assertTrue(confirmed["saved"])
        repeated = service.confirm(
            "running", 5.0, self.today, "field_test", str(preview["preview_hash"]),
            activity.record_id,
        )
        self.assertTrue(repeated["already_confirmed"])
        self.assertEqual(service.get("running")["reference_bands"]["easy"]["min"], 5.75)
        older = SportCalibrationService("a1", self.records, today=self.today + timedelta(days=121))
        self.assertEqual(older.get("running")["status"], "stale")
        self.assertIsNone(older.get("running")["reference_bands"])
        other = SportCalibrationService("a2", self.records, today=self.today)
        with self.assertRaisesRegex(ValueError, "source activity"):
            other.preview("running", 5.0, self.today, "field_test", activity.record_id)

    def test_plan_guidance_is_bound_to_confirmed_calibration(self) -> None:
        goal_id = self._goal()
        calibration = SportCalibrationService("a1", self.records, today=self.today)
        preview = calibration.preview("running", 5.0, self.today, "field_test")
        calibration.confirm("running", 5.0, self.today, "field_test", str(preview["preview_hash"]))
        plan = self.workflow.preview_plan(
            goal_id=goal_id,
            start_date=self.today,
            constraints=TrainingConstraints(
                {self.today.weekday(): 45}, frozenset(), 45, ("running",)
            ),
        )
        self.assertTrue(plan["intensity_guidance"])
        self.assertEqual(
            plan["planning_context"]["calibration_record_ids"]["running"],
            calibration.get("running")["record_id"],
        )

    def test_execution_requires_user_confirmation_and_affects_review(self) -> None:
        plan = self._plan()
        plan_id = str(plan["plan_id"])
        session_id = str(plan["sessions"][0]["session_id"])
        activity = CanonicalRecord.create(
            athlete_id="a1", kind="activity", occurred_at=self.now,
            data={"sport": "running", "duration_min": 30, "distance_m": 6000},
        )
        self.records.save(activity)
        service = PlanExecutionService("a1", self.records, timezone_name="UTC")
        suggested = service.list(plan_id)["pending_reviews"]
        self.assertEqual(suggested[0]["match_state"], "suggested")
        self.assertEqual(suggested[0]["activity_candidates"][0]["activity_id"], activity.record_id)
        values = {
            "plan_id": plan_id, "session_id": session_id, "status": "partial",
            "activity_id": activity.record_id, "rpe": 8, "pain_after": 5,
            "reason": "stopped after pain",
        }
        preview = service.preview(**values)
        self.assertEqual(service.list(plan_id)["count"], 0)
        with self.assertRaisesRegex(ValueError, "preview again"):
            service.confirm(preview_hash="wrong", **values)
        saved = service.confirm(preview_hash=str(preview["preview_hash"]), **values)
        self.assertTrue(saved["saved"])
        self.assertEqual(service.list(plan_id)["count"], 1)
        self.assertEqual(service.list(plan_id)["pending_reviews"], [])
        review = self.workflow.review_plan(plan_id)
        self.assertEqual(review["confirmed_executions"][0]["status"], "partial")
        self.assertEqual(review["possible_activity_changes"], [])
        blocked = self.workflow.preview_plan_update(plan_id=plan_id)
        self.assertTrue(blocked["blocked"])
        self.assertIn("pain", blocked["reason"])

    def test_execution_candidates_remain_ambiguous_until_athlete_chooses(self) -> None:
        plan = self._plan()
        plan_id = str(plan["plan_id"])
        session_id = str(plan["sessions"][0]["session_id"])
        activities = [
            CanonicalRecord.create(
                athlete_id="a1", kind="activity", occurred_at=self.now + timedelta(minutes=index),
                data={"sport": "running", "duration_min": 30 + index},
            )
            for index in (0, 1)
        ]
        for activity in activities:
            self.records.save(activity)
        service = PlanExecutionService("a1", self.records, timezone_name="UTC")
        pending = service.list(plan_id)["pending_reviews"]
        self.assertEqual(pending[0]["match_state"], "ambiguous")
        self.assertEqual(len(pending[0]["activity_candidates"]), 2)
        self.assertEqual(service.list(plan_id)["count"], 0)
        values = {
            "plan_id": plan_id, "session_id": session_id, "status": "completed",
            "activity_id": activities[0].record_id, "rpe": 5, "pain_after": 0,
            "reason": None,
        }
        preview = service.preview(**values)
        service.confirm(preview_hash=str(preview["preview_hash"]), **values)
        self.assertEqual(service.list(plan_id)["pending_reviews"], [])

    def test_moved_day_suggestion_and_extra_activity_are_separate(self) -> None:
        plan = self._plan()
        plan_id = str(plan["plan_id"])
        moved = CanonicalRecord.create(
            athlete_id="a1", kind="activity", occurred_at=self.now + timedelta(days=1),
            data={"sport": "running", "duration_min": 42},
        )
        extra = CanonicalRecord.create(
            athlete_id="a1", kind="activity", occurred_at=self.now + timedelta(days=1, hours=1),
            data={"sport": "cycling", "duration_min": 30},
        )
        self.records.save(moved)
        self.records.save(extra)
        service = PlanExecutionService(
            "a1", self.records, timezone_name="UTC", now=self.now + timedelta(days=2)
        )
        result = service.list(plan_id)
        self.assertEqual(result["pending_reviews"][0]["match_state"], "suggested_moved")
        self.assertEqual(result["pending_reviews"][0]["activity_candidates"][0]["activity_id"],
                         moved.record_id)
        self.assertEqual(result["unplanned_activities"][0]["activity_id"], extra.record_id)

    def test_multiweek_forecast_has_recovery_and_event_ceiling_only(self) -> None:
        start = self.today + timedelta(days=1)
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
            raw,
            observed_first_week_minutes=180,
            goal_type="race",
            goal_deadline=start + timedelta(days=32),
        )
        weeks = cycle["forecast"]
        self.assertEqual(weeks[0]["weekly_ceiling_min"], 180)
        self.assertEqual(weeks[3]["phase"], "taper_forecast")
        self.assertLess(weeks[3]["weekly_ceiling_min"], weeks[2]["weekly_ceiling_min"])
        self.assertEqual(weeks[4]["phase"], "event_week_review")
        self.assertTrue(all(week["status"] == "forecast_only" for week in weeks[1:]))
        self.assertTrue(all(week["requires_fresh_evidence_and_confirmation"] for week in weeks[1:]))

    def test_taper_week_reduces_volume_and_removes_key_sessions(self) -> None:
        event_day = self.today + timedelta(days=10)
        proposal = self.workflow.propose_goal(
            idea="10K race", goal_type="race", deadline=event_day
        )
        goal_id = str(self.workflow.confirm_goal(
            str(proposal["proposal_id"]), int(proposal["revision"])
        )["goal"]["goal_id"])
        preview = self.workflow.preview_plan(
            goal_id=goal_id,
            start_date=self.today,
            constraints=TrainingConstraints(
                {day: 60 for day in range(7)}, frozenset(), 300, ("running",),
                preferred_quality_weekday=self.today.weekday(),
                preferred_long_weekday=(self.today + timedelta(days=3)).weekday(),
            ),
        )
        self.assertLessEqual(preview["summary"]["total_minutes"], 240)
        self.assertTrue(preview["planning_context"]["taper_week"])
        self.assertEqual(preview["summary"]["key_sessions"], 0)

    def test_non_race_block_still_has_recovery_week(self) -> None:
        start = self.today
        raw = {
            "base_constraints": {"max_weekly_minutes": 300},
            "forecast": [
                {
                    "start_date": (start + timedelta(days=7 * index)).isoformat(),
                    "end_date": (start + timedelta(days=7 * index + 6)).isoformat(),
                }
                for index in range(4)
            ],
        }
        cycle = add_progression_envelope(
            raw, observed_first_week_minutes=180, goal_type="fitness", goal_deadline=None
        )
        self.assertEqual(cycle["forecast"][3]["phase"], "recovery_forecast")

    def test_race_week_reserves_event_day_and_removes_key_training(self) -> None:
        event_day = self.today + timedelta(days=4)
        proposal = self.workflow.propose_goal(
            idea="10K race", goal_type="race", deadline=event_day
        )
        goal_id = str(self.workflow.confirm_goal(
            str(proposal["proposal_id"]), int(proposal["revision"])
        )["goal"]["goal_id"])
        preview = self.workflow.preview_plan(
            goal_id=goal_id,
            start_date=self.today,
            constraints=TrainingConstraints(
                {day: 60 for day in range(7)}, frozenset(), 300, ("running",),
                preferred_quality_weekday=self.today.weekday(),
                preferred_long_weekday=event_day.weekday(),
            ),
        )
        self.assertLessEqual(preview["summary"]["total_minutes"], 180)
        self.assertEqual(preview["planning_context"]["event_day"], event_day.isoformat())
        self.assertTrue(all(
            session["session_date"] != event_day.isoformat()
            and session["session_type"] not in {"quality", "long"}
            for session in preview["plan"]["sessions"]
        ))


if __name__ == "__main__":
    unittest.main()
