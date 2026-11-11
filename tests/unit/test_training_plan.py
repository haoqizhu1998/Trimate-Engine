from __future__ import annotations

import unittest
from datetime import date, timedelta

from athlete_growth.application.plan import service
from athlete_growth.domain.goals import Goal
from athlete_growth.domain.safety import RiskLevel, SafetyAssessment
from athlete_growth.domain.training import models


class TrainingPlanTests(unittest.TestCase):
    def test_generation_respects_time_and_unavailable_day_constraints(self) -> None:
        planner_type = getattr(service, "TrainingPlanner", None)
        constraints_type = getattr(models, "TrainingConstraints", None)
        self.assertIsNotNone(planner_type)
        self.assertIsNotNone(constraints_type)
        constraints = constraints_type(
            available_minutes_by_weekday={0: 45, 2: 60, 5: 90},
            unavailable_weekdays=frozenset({2}),
            max_weekly_minutes=120,
            allowed_sports=("running",),
        )
        goal = Goal("g1", "athlete-001", "race", "10K", 5, date(2027, 1, 1))
        safety = SafetyAssessment(RiskLevel.LOW, (), (), (), ())

        plan = planner_type().generate(
            athlete_id="athlete-001",
            goal=goal,
            constraints=constraints,
            safety=safety,
            start_date=date(2026, 10, 26),
        )

        self.assertLessEqual(sum(session.duration_min for session in plan.sessions), 120)
        self.assertTrue(all(session.session_date.weekday() != 2 for session in plan.sessions))
        self.assertTrue(all(session.objective for session in plan.sessions))
        self.assertTrue(all(session.expected_load >= 0 for session in plan.sessions))
        self.assertEqual(len(plan.sessions), 2)
        self.assertTrue(all(session.duration_min <= 30 for session in plan.sessions))
        self.assertTrue(all(session.session_type == "aerobic" for session in plan.sessions))

    def test_high_risk_generation_never_adds_training_load(self) -> None:
        planner_type = getattr(service, "TrainingPlanner", None)
        constraints_type = getattr(models, "TrainingConstraints", None)
        self.assertIsNotNone(planner_type)
        constraints = constraints_type({0: 60}, frozenset(), 60, ("running",))
        goal = Goal("g1", "athlete-001", "sport", "Consistency", 3, None)
        safety = SafetyAssessment(RiskLevel.HIGH, ("pain",), ("rest",), (), ())

        plan = planner_type().generate(
            athlete_id="athlete-001",
            goal=goal,
            constraints=constraints,
            safety=safety,
            start_date=date(2026, 10, 26),
        )

        self.assertEqual(plan.sessions, ())
        self.assertIn("no training prescribed", plan.change_reason)

    def test_multisport_allocation_uses_each_requested_discipline(self) -> None:
        constraints = models.TrainingConstraints(
            available_minutes_by_weekday={0: 45, 1: 60, 2: 50, 3: 45},
            unavailable_weekdays=frozenset(),
            max_weekly_minutes=200,
            allowed_sports=("swimming", "cycling", "running"),
        )
        goal = Goal("g2", "athlete-001", "race", "Triathlon", 5, date(2027, 1, 1))
        plan = service.TrainingPlanner().generate(
            athlete_id="athlete-001",
            goal=goal,
            constraints=constraints,
            safety=SafetyAssessment(RiskLevel.LOW, (), (), (), ()),
            start_date=date(2026, 10, 26),
        )
        self.assertEqual(
            [session.sport for session in plan.sessions],
            ["swimming", "cycling", "running"],
        )

    def test_recent_week_not_old_peak_sets_volume_ceiling(self) -> None:
        start = date(2026, 10, 26)
        plan = service.TrainingPlanner().generate(
            athlete_id="athlete-001",
            goal=Goal("g3", "athlete-001", "sport", "Consistency", 3, None),
            constraints=models.TrainingConstraints(
                {weekday: 60 for weekday in range(7)},
                frozenset(), 300, ("running",),
            ),
            safety=SafetyAssessment(RiskLevel.LOW, (), (), (), ()),
            start_date=start,
            recent_sessions=(
                ("running", 120, start - timedelta(days=20)),
                ("running", 20, start - timedelta(days=2)),
            ),
        )
        self.assertLessEqual(sum(item.duration_min for item in plan.sessions), 20)

    def test_restart_after_gap_is_short_easy_preview(self) -> None:
        start = date(2026, 10, 26)
        plan = service.TrainingPlanner().generate(
            athlete_id="athlete-001",
            goal=Goal("g4", "athlete-001", "race", "10K", 3, None),
            constraints=models.TrainingConstraints(
                {weekday: 90 for weekday in range(7)},
                frozenset(), 400, ("running",),
                preferred_quality_weekday=1, preferred_long_weekday=5,
            ),
            safety=SafetyAssessment(RiskLevel.LOW, (), (), (), ()),
            start_date=start,
            recent_sessions=(("running", 90, start - timedelta(days=21)),),
        )
        self.assertLessEqual(len(plan.sessions), 2)
        self.assertTrue(all(item.duration_min <= 30 for item in plan.sessions))
        self.assertTrue(all(item.session_type == "aerobic" for item in plan.sessions))

    def test_recent_other_sport_does_not_restore_old_focus_duration(self) -> None:
        start = date(2026, 10, 26)
        plan = service.TrainingPlanner().generate(
            athlete_id="athlete-001",
            goal=Goal("g5", "athlete-001", "sport", "Triathlon", 3, None),
            constraints=models.TrainingConstraints(
                {weekday: 90 for weekday in range(7)},
                frozenset(), 180, ("running", "cycling"), focus_sport="running",
                preferred_long_weekday=5,
            ),
            safety=SafetyAssessment(RiskLevel.LOW, (), (), (), ()),
            start_date=start,
            recent_sessions=(
                ("running", 90, start - timedelta(days=10)),
                ("cycling", 120, start - timedelta(days=2)),
            ),
        )
        self.assertTrue(
            all(item.duration_min <= 30 for item in plan.sessions if item.sport == "running")
        )
        self.assertFalse(any(item.session_type == "long" for item in plan.sessions))

    def test_sustained_training_can_build_above_last_seven_days(self) -> None:
        start = date(2026, 10, 26)
        sessions = tuple(
            ("running", 50, start - timedelta(days=age))
            for age in (2, 4, 6, 9, 11, 13, 16, 18, 20)
        )
        constraints = models.TrainingConstraints(
            {weekday: 65 for weekday in range(7)}, frozenset(), 250, ("running",)
        )
        safety = SafetyAssessment(RiskLevel.LOW, (), (), (), ())
        planner = service.TrainingPlanner()
        decision = planner.volume_decision(
            start_date=start, recent_sessions=sessions,
            constraints=constraints, safety=safety,
        )
        plan = planner.generate(
            athlete_id="athlete-001",
            goal=Goal("g6", "athlete-001", "race", "Half marathon", 3, None),
            constraints=constraints, safety=safety, start_date=start,
            recent_sessions=sessions,
        )
        self.assertEqual(decision.mode, "progressive_build")
        self.assertEqual(decision.observed_week_minutes, (150, 150, 150, 0))
        self.assertGreater(sum(item.duration_min for item in plan.sessions), 150)
        self.assertLessEqual(sum(item.duration_min for item in plan.sessions), 250)
        self.assertLessEqual(len(plan.sessions), 4)

    def test_one_lighter_week_can_return_toward_established_volume(self) -> None:
        start = date(2026, 10, 26)
        sessions = (
            ("running", 55, start - timedelta(days=2)),
            ("running", 55, start - timedelta(days=5)),
            *(("running", 70, start - timedelta(days=age))
              for age in (9, 11, 13, 16, 18, 20)),
        )
        constraints = models.TrainingConstraints(
            {weekday: 70 for weekday in range(7)}, frozenset(), 300, ("running",)
        )
        decision = service.TrainingPlanner.volume_decision(
            start_date=start, recent_sessions=sessions, constraints=constraints,
            safety=SafetyAssessment(RiskLevel.LOW, (), (), (), ()),
        )
        self.assertEqual(decision.mode, "return_to_baseline")
        self.assertGreater(decision.target_minutes, 110)
        self.assertLess(decision.target_minutes, 210)

    def test_spike_or_unknown_readiness_does_not_authorize_progression(self) -> None:
        start = date(2026, 10, 26)
        sessions = (
            *(("running", 100, start - timedelta(days=age)) for age in (2, 4, 6)),
            *(("running", 30, start - timedelta(days=age))
              for age in (9, 11, 13, 16, 18, 20)),
        )
        constraints = models.TrainingConstraints(
            {weekday: 90 for weekday in range(7)}, frozenset(), 400, ("running",)
        )
        low = service.TrainingPlanner.volume_decision(
            start_date=start, recent_sessions=sessions, constraints=constraints,
            safety=SafetyAssessment(RiskLevel.LOW, (), (), (), ()),
        )
        unknown = service.TrainingPlanner.volume_decision(
            start_date=start, recent_sessions=sessions, constraints=constraints,
            safety=SafetyAssessment(RiskLevel.UNKNOWN, (), (), (), ()),
        )
        self.assertEqual(low.mode, "recent_spike_hold")
        self.assertEqual(low.target_minutes, 300)
        self.assertEqual(unknown.mode, "safety_hold")
        self.assertEqual(unknown.target_minutes, 300)

    def test_inactive_middle_week_does_not_count_as_continuous_training(self) -> None:
        start = date(2026, 10, 26)
        sessions = (
            *(("running", 50, start - timedelta(days=age)) for age in (2, 4, 6)),
            *(("running", 50, start - timedelta(days=age)) for age in (16, 18, 20)),
            *(("running", 50, start - timedelta(days=age)) for age in (23, 25, 27)),
        )
        decision = service.TrainingPlanner.volume_decision(
            start_date=start, recent_sessions=sessions,
            constraints=models.TrainingConstraints(
                {weekday: 60 for weekday in range(7)},
                frozenset(), 250, ("running",),
            ),
            safety=SafetyAssessment(RiskLevel.LOW, (), (), (), ()),
        )
        self.assertEqual(decision.observed_week_minutes, (150, 0, 150, 150))
        self.assertEqual(decision.mode, "recent_hold")
        self.assertEqual(decision.target_minutes, 150)


if __name__ == "__main__":
    unittest.main()
