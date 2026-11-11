from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from athlete_growth.adapters.storage import SQLitePlanningDraftRepository, SQLiteRecordStore
from athlete_growth.application.plan.execution import PlanExecutionService
from athlete_growth.application.plan.workflow import PlanningWorkflow
from athlete_growth.domain.records import CanonicalRecord
from athlete_growth.domain.training import TrainingConstraints


class PlanningWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        database = Path(self.temporary.name) / "athlete.sqlite3"
        self.records = SQLiteRecordStore(database)
        self.drafts = SQLitePlanningDraftRepository(database, self.records)
        self.now = datetime(2026, 10, 27, 9, tzinfo=UTC)
        self.workflow = PlanningWorkflow("athlete-1", self.records, self.drafts, clock=self.now)

    def _confirmed_goal(self) -> str:
        proposal = self.workflow.propose_goal(
            idea="我想完成第一次 10 公里比赛",
            goal_type="race",
            deadline=date(2027, 1, 1),
        )
        confirmed = self.workflow.confirm_goal(
            str(proposal["proposal_id"]), int(proposal["revision"])
        )
        goal = confirmed["goal"]
        assert isinstance(goal, dict)
        return str(goal["goal_id"])

    def _constraints(self) -> TrainingConstraints:
        return TrainingConstraints({1: 40, 3: 50}, frozenset(), 90, ("running",))

    def test_goal_idea_needs_type_and_user_confirmation(self) -> None:
        proposal = self.workflow.propose_goal(idea="我想开始规律运动")
        self.assertEqual(proposal["missing_fields"], ["goal_type"])
        self.assertEqual(len(self.workflow.list_open_drafts()["drafts"]), 1)
        self.assertEqual(self.records.list_current("athlete-1", kind="goal"), [])
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.workflow.confirm_goal(str(proposal["proposal_id"]), 1)

        revised = self.workflow.propose_goal(
            proposal_id=str(proposal["proposal_id"]),
            expected_revision=1,
            goal_type="sport",
            title="每周规律运动",
        )
        self.assertEqual(revised["revision"], 2)
        with self.assertRaisesRegex(ValueError, "revision"):
            self.workflow.confirm_goal(str(proposal["proposal_id"]), 1)
        confirmed = self.workflow.confirm_goal(str(proposal["proposal_id"]), 2)
        repeated = self.workflow.confirm_goal(str(proposal["proposal_id"]), 2)
        self.assertEqual(confirmed["record_id"], repeated["record_id"])
        self.assertEqual(len(self.records.list_current("athlete-1", kind="goal")), 1)
        self.assertEqual(self.workflow.list_open_drafts()["drafts"], [])

    def test_plan_preview_requires_confirmation_and_uses_conservative_intensity(self) -> None:
        goal_id = self._confirmed_goal()
        preview = self.workflow.preview_plan(
            goal_id=goal_id,
            start_date=date(2026, 10, 27),
            constraints=self._constraints(),
        )
        self.assertFalse(preview["blocked"])
        self.assertEqual(self.records.list_current("athlete-1", kind="training-plan"), [])
        self.assertEqual(len(self.workflow.list_open_drafts()["drafts"]), 1)
        plan = preview["plan"]
        assert isinstance(plan, dict)
        self.assertTrue(all(item["intensity"] == "easy" for item in plan["sessions"]))

        confirmed = self.workflow.confirm_plan(str(preview["preview_id"]), 1)
        repeated = self.workflow.confirm_plan(str(preview["preview_id"]), 1)
        self.assertEqual(confirmed["record_id"], repeated["record_id"])
        saved = self.records.list_current("athlete-1", kind="training-plan")
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].data["status"], "approved")
        self.assertTrue(all(item["status"] == "approved" for item in saved[0].data["sessions"]))
        self.assertEqual(self.workflow.list_open_drafts()["drafts"], [])

    def test_exact_double_days_brick_and_discipline_distances_persist(self) -> None:
        goal_id = self._confirmed_goal()
        constraints = TrainingConstraints(
            {1: 80, 2: 110}, frozenset(), 190,
            ("swimming", "cycling", "running"),
        )
        requested = (
            {"session_date": "2026-10-27", "sport": "swimming", "duration_min": 30,
             "distance_m": 1500},
            {"session_date": "2026-10-27", "sport": "running", "duration_min": 40,
             "distance_m": 7000},
            {"session_date": "2026-10-28", "sport": "cycling", "duration_min": 80,
             "distance_m": 30000, "session_type": "brick", "brick_id": "b1"},
            {"session_date": "2026-10-28", "sport": "running", "duration_min": 20,
             "distance_m": 3000, "session_type": "brick", "brick_id": "b1"},
        )
        preview = self.workflow.preview_plan(
            goal_id=goal_id, start_date=date(2026, 10, 27),
            constraints=constraints, requested_sessions=requested,
        )
        self.assertEqual(preview["summary"]["total_minutes"], 170)
        self.assertEqual(preview["summary"]["per_sport_distance_m"], {
            "swimming": 1500, "cycling": 30000, "running": 10000,
        })
        self.assertEqual(preview["summary"]["brick_count"], 1)
        self.assertEqual(len(preview["summary"]["two_session_days"]), 2)
        sessions = preview["plan"]["sessions"]
        self.assertEqual([item["session_order"] for item in sessions], [1, 2, 1, 2])
        self.assertEqual([item["sport"] for item in sessions[2:]], ["cycling", "running"])
        saved = self.workflow.confirm_plan(str(preview["preview_id"]), 1)["plan"]
        self.assertEqual(
            [(item["distance_m"], item["brick_id"]) for item in saved["sessions"]],
            [(1500, None), (7000, None), (30000, "b1"), (3000, "b1")],
        )
        restored = self.workflow._plan_from_data(saved)
        self.assertEqual(restored.sessions[3].distance_m, 3000)
        self.assertEqual(restored.sessions[3].brick_id, "b1")
        pending = PlanExecutionService(
            "athlete-1", self.records, timezone_name="UTC",
            now=self.now + timedelta(days=1),
        ).list(str(saved["plan_id"]))["pending_reviews"]
        self.assertEqual(
            [item["planned_distance_m"] for item in pending], [1500, 7000, 30000, 3000]
        )
        self.assertEqual([item["session_order"] for item in pending], [1, 2, 1, 2])
        self.assertEqual([item["brick_id"] for item in pending], [None, None, "b1", "b1"])
        shortened = self.workflow.preview_plan_adjustment(
            plan_id=str(saved["plan_id"]),
            session_id=str(saved["sessions"][1]["session_id"]),
            action="shorten", reason="Less time today", new_duration_min=30,
        )
        self.assertIsNone(shortened["plan"]["sessions"][1]["distance_m"])
        self.assertIsNone(shortened["summary"]["per_sport_distance_m"]["running"])
        self.assertEqual(shortened["summary"]["known_per_sport_distance_m"]["running"], 3000)
        with self.assertRaisesRegex(ValueError, "both brick legs"):
            self.workflow.preview_plan_adjustment(
                plan_id=str(saved["plan_id"]),
                session_id=str(saved["sessions"][2]["session_id"]),
                action="skip", reason="Schedule conflict",
            )

    def test_exact_schedule_rejects_daily_overflow_and_broken_brick(self) -> None:
        goal_id = self._confirmed_goal()
        constraints = TrainingConstraints(
            {1: 60}, frozenset(), 120, ("cycling", "running"),
        )
        with self.assertRaisesRegex(ValueError, "available minutes"):
            self.workflow.preview_plan(
                goal_id=goal_id, start_date=date(2026, 10, 27),
                constraints=constraints,
                requested_sessions=(
                    {"session_date": "2026-10-27", "sport": "cycling", "duration_min": 45},
                    {"session_date": "2026-10-27", "sport": "running", "duration_min": 30},
                ),
            )
        with self.assertRaisesRegex(ValueError, "consecutive cycling then running"):
            self.workflow.preview_plan(
                goal_id=goal_id, start_date=date(2026, 10, 27),
                constraints=constraints,
                requested_sessions=(
                    {"session_date": "2026-10-27", "sport": "running",
                     "duration_min": 20, "session_type": "brick", "brick_id": "b"},
                    {"session_date": "2026-10-27", "sport": "cycling",
                     "duration_min": 30, "session_type": "brick", "brick_id": "b"},
                ),
            )

    def test_distance_only_and_time_only_targets_remain_unspecified_where_missing(self) -> None:
        goal_id = self._confirmed_goal()
        constraints = TrainingConstraints(
            {}, frozenset(), 0, ("cycling", "running", "swimming")
        )
        requested = (
            {"session_date": "2026-10-27", "sport": "cycling",
             "distance_m": 40000, "session_type": "brick", "brick_id": "transition"},
            {"session_date": "2026-10-27", "sport": "running",
             "distance_m": 5000, "session_type": "brick", "brick_id": "transition"},
            {"session_date": "2026-10-28", "sport": "swimming", "duration_min": 45},
        )
        preview = self.workflow.preview_plan(
            goal_id=goal_id, start_date=date(2026, 10, 27),
            constraints=constraints, requested_sessions=requested,
        )
        sessions = preview["plan"]["sessions"]
        self.assertEqual([item["duration_min"] for item in sessions], [None, None, 45])
        self.assertEqual([item["distance_m"] for item in sessions], [40000, 5000, None])
        self.assertEqual([item["expected_load"] for item in sessions], [None, None, 45.0])
        self.assertIsNone(preview["summary"]["total_minutes"])
        self.assertEqual(preview["summary"]["known_minutes"], 45)
        self.assertEqual(preview["summary"]["duration_missing_sessions"], 2)
        self.assertEqual(preview["summary"]["per_sport_minutes"], {
            "cycling": None, "running": None, "swimming": 45,
        })
        self.assertEqual(preview["summary"]["per_sport_distance_m"], {
            "cycling": 40000, "running": 5000, "swimming": None,
        })
        self.assertFalse(preview["planning_context"]["weekly_time_limit_known"])
        self.assertEqual(len(preview["planning_context"]["daily_time_limits_missing_days"]), 2)
        saved = self.workflow.confirm_plan(str(preview["preview_id"]), 1)["plan"]
        restored = self.workflow._plan_from_data(saved)
        self.assertIsNone(restored.sessions[0].duration_min)
        self.assertEqual(restored.sessions[0].distance_m, 40000)
        revised = self.workflow.preview_plan_update(
            plan_id=str(saved["plan_id"]), start_date=date(2026, 10, 27),
            requested_sessions=requested,
        )
        self.assertIsNone(revised["summary"]["total_minutes"])
        self.assertIsNone(revised["replanning_context"]["minutes_change"])
        blocks = self.workflow.preview_plan_blocks(
            goal_id=goal_id, start_date=date(2026, 10, 27),
            constraints=constraints, weeks=2,
            focus_sequence=("cycling", "running"), requested_sessions=requested,
        )
        self.assertIsNone(blocks["block_cycle"]["forecast"][1]["weekly_ceiling_min"])

    def test_session_requires_a_time_or_distance_target(self) -> None:
        goal_id = self._confirmed_goal()
        with self.assertRaisesRegex(ValueError, "duration_min or distance_m"):
            self.workflow.preview_plan(
                goal_id=goal_id, start_date=date(2026, 10, 27),
                constraints=TrainingConstraints({}, frozenset(), 0, ("running",)),
                requested_sessions=({"session_date": "2026-10-27", "sport": "running"},),
            )

    def test_plan_baseline_normalizes_sport_and_excludes_flagged_activity(self) -> None:
        goal_id = self._confirmed_goal()
        self.records.save(
            CanonicalRecord.create(
                athlete_id="athlete-1", kind="activity",
                occurred_at=datetime(2026, 10, 26, 9, tzinfo=UTC),
                data={"sport": "road_running", "duration_min": 20},
            )
        )
        self.records.save(
            CanonicalRecord.create(
                athlete_id="athlete-1", kind="activity",
                occurred_at=datetime(2026, 10, 26, 10, tzinfo=UTC),
                data={"sport": "running", "duration_min": 500},
                quality_flags=("invalid:duration_min",),
            )
        )
        preview = self.workflow.preview_plan(
            goal_id=goal_id,
            start_date=date(2026, 10, 27),
            constraints=self._constraints(),
        )
        self.assertEqual(preview["planning_context"]["recent_7d_minutes"], 20)
        self.assertEqual(preview["planning_context"]["excluded_activity_count"], 1)
        self.assertLessEqual(preview["summary"]["total_minutes"], 20)

    def test_established_training_previews_more_than_last_week_when_ready(self) -> None:
        goal_id = self._confirmed_goal()
        self.records.save(CanonicalRecord.create(
            athlete_id="athlete-1", kind="check-in", occurred_at=self.now,
            data={"pain": 0, "fatigue": 2, "soreness": 2, "illness": False},
        ))
        for age in (2, 4, 6, 9, 11, 13, 16, 18, 20):
            self.records.save(CanonicalRecord.create(
                athlete_id="athlete-1", kind="activity",
                occurred_at=self.now - timedelta(days=age),
                data={"sport": "running", "duration_min": 50},
            ))
        preview = self.workflow.preview_plan(
            goal_id=goal_id, start_date=date(2026, 10, 27),
            constraints=TrainingConstraints(
                {weekday: 60 for weekday in range(7)},
                frozenset(), 250, ("running",),
            ),
        )
        self.assertEqual(preview["planning_context"]["baseline_mode"], "progressive_build")
        self.assertEqual(preview["planning_context"]["observed_week_minutes"][:3], [150] * 3)
        self.assertGreater(preview["summary"]["total_minutes"], 150)
        self.assertLessEqual(
            preview["summary"]["total_minutes"],
            preview["planning_context"]["weekly_volume_budget_minutes"],
        )

    def test_changed_data_invalidates_plan_preview(self) -> None:
        goal_id = self._confirmed_goal()
        preview = self.workflow.preview_plan(
            goal_id=goal_id,
            start_date=date(2026, 10, 27),
            constraints=self._constraints(),
        )
        self.records.save(
            CanonicalRecord.create(
                athlete_id="athlete-1",
                kind="activity",
                occurred_at=self.now,
                data={"sport": "running", "duration_min": 30},
            )
        )
        with self.assertRaisesRegex(ValueError, "source data changed"):
            self.workflow.confirm_plan(str(preview["preview_id"]), 1)
        self.assertEqual(self.records.list_current("athlete-1", kind="training-plan"), [])

    def test_high_risk_blocks_plan_preview(self) -> None:
        goal_id = self._confirmed_goal()
        self.records.save(
            CanonicalRecord.create(
                athlete_id="athlete-1",
                kind="check-in",
                occurred_at=self.now,
                data={"pain": 8, "fatigue": 3, "soreness": 2, "illness": False},
            )
        )
        result = self.workflow.preview_plan(
            goal_id=goal_id,
            start_date=date(2026, 10, 27),
            constraints=self._constraints(),
        )
        self.assertTrue(result["blocked"])
        self.assertEqual(result["risk"]["level"], "high")
        self.assertEqual(self.records.list_current("athlete-1", kind="training-plan"), [])

    def test_other_profile_cannot_confirm_draft(self) -> None:
        proposal = self.workflow.propose_goal(idea="Improve", goal_type="sport")
        other = PlanningWorkflow("athlete-2", self.records, self.drafts, clock=self.now)
        with self.assertRaisesRegex(KeyError, "draft not found"):
            other.confirm_goal(str(proposal["proposal_id"]), 1)

    def test_replace_goal_is_atomic_and_requires_plan_review(self) -> None:
        old_goal_id = self._confirmed_goal()
        preview = self.workflow.preview_plan(
            goal_id=old_goal_id,
            start_date=date(2026, 10, 27),
            constraints=self._constraints(),
        )
        plan = self.workflow.confirm_plan(str(preview["preview_id"]), 1)["plan"]
        assert isinstance(plan, dict)
        change = self.workflow.preview_goal_change(
            action="replace",
            goal_id=old_goal_id,
            reason="change of focus",
            goal_type="sport",
            title="Build running consistency",
            priority=2,
        )
        self.assertEqual(len(self.workflow._goals()), 1)
        self.assertIn(str(plan["plan_id"]), change["affected_plan_ids"])
        saved = self.workflow.confirm_goal_change(str(change["preview_id"]), 1)
        self.assertTrue(saved["confirmed"])
        self.assertEqual(
            saved["record_id"],
            self.workflow.confirm_goal_change(str(change["preview_id"]), 1)["record_id"],
        )
        self.assertNotIn(old_goal_id, [goal.goal_id for goal in self.workflow._goals()])
        review = self.workflow.review_plan(str(plan["plan_id"]))
        self.assertTrue(review["review_required"])
        self.assertIn("plan goal was replaced or deleted", review["reasons"])
        self.assertFalse(review["automatic_change_applied"])
        replacement_goal = saved["goal"]
        assert isinstance(replacement_goal, dict)
        updated = self.workflow.preview_plan_update(
            plan_id=str(plan["plan_id"]),
            goal_id=str(replacement_goal["goal_id"]),
        )
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["replanning_context"]["previous_plan_id"], plan["plan_id"])
        self.assertEqual(len(self.records.list_current("athlete-1", kind="training-plan")), 1)
        approved = self.workflow.confirm_plan(str(updated["preview_id"]), 2)
        self.assertTrue(approved["confirmed"])
        self.assertEqual(len(self.records.list_current("athlete-1", kind="training-plan")), 2)

    def test_delete_goal_is_soft_and_stale_preview_cannot_apply(self) -> None:
        old_goal_id = self._confirmed_goal()
        change = self.workflow.preview_goal_change(
            action="delete", goal_id=old_goal_id, reason="no longer relevant"
        )
        self._confirmed_goal()
        with self.assertRaisesRegex(ValueError, "source data changed"):
            self.workflow.confirm_goal_change(str(change["preview_id"]), 1)
        fresh = self.workflow.preview_goal_change(
            action="delete", goal_id=old_goal_id, reason="no longer relevant"
        )
        result = self.workflow.confirm_goal_change(str(fresh["preview_id"]), 1)
        self.assertIsNone(result["goal"])
        self.assertNotIn(old_goal_id, [goal.goal_id for goal in self.workflow._goals()])
        self.assertIsNotNone(self.records.get(str(result["record_id"])))

    def test_plan_caps_unobserved_sport_and_separates_key_sessions(self) -> None:
        goal_id = self._confirmed_goal()
        for sport, minutes in (("running", 60), ("cycling", 90)):
            self.records.save(
                CanonicalRecord.create(
                    athlete_id="athlete-1",
                    kind="activity",
                    occurred_at=datetime(2026, 10, 26, 9, tzinfo=UTC),
                    data={"sport": sport, "duration_min": minutes},
                )
            )
        self.records.save(
            CanonicalRecord.create(
                athlete_id="athlete-1",
                kind="check-in",
                occurred_at=self.now,
                data={"pain": 0, "fatigue": 2, "soreness": 2, "illness": False},
            )
        )
        # The preferred key days are adjacent, so a quality session is omitted.
        constraints = TrainingConstraints(
            {0: 50, 1: 50, 2: 50, 3: 50, 4: 50},
            frozenset(),
            250,
            ("running", "cycling", "swimming"),
            preferred_quality_weekday=1,
            preferred_long_weekday=2,
            focus_sport="running",
        )
        preview = self.workflow.preview_plan(
            goal_id=goal_id,
            start_date=date(2026, 10, 27),
            constraints=constraints,
        )
        plan = preview["plan"]
        assert isinstance(plan, dict)
        sessions = plan["sessions"]
        self.assertFalse(any(item["session_type"] == "quality" for item in sessions))
        self.assertLessEqual(
            max(item["duration_min"] for item in sessions if item["sport"] == "swimming"), 45
        )
        long_session = next(item for item in sessions if item["session_type"] == "long")
        recovery = next(item for item in sessions if item["session_type"] == "recovery")
        self.assertLessEqual(recovery["duration_min"], long_session["duration_min"] // 2)
        self.assertEqual(
            preview["summary"]["total_minutes"], sum(item["duration_min"] for item in sessions)
        )


if __name__ == "__main__":
    unittest.main()
