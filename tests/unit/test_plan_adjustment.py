from __future__ import annotations

import unittest
from datetime import date

from athlete_growth.application.plan import service
from athlete_growth.domain.goals import Goal
from athlete_growth.domain.safety import RiskLevel, SafetyAssessment
from athlete_growth.domain.training.models import TrainingConstraints
from athlete_growth.ports import workout_sink


class PlanAdjustmentTests(unittest.TestCase):
    def test_adjustment_creates_new_version_without_rewriting_history(self) -> None:
        planner_type = getattr(service, "TrainingPlanner", None)
        self.assertIsNotNone(planner_type)
        planner = planner_type()
        constraints = TrainingConstraints({0: 60, 2: 60}, frozenset(), 120, ("running",))
        goal = Goal("g1", "athlete-001", "race", "10K", 5, date(2027, 1, 1))
        low = SafetyAssessment(RiskLevel.LOW, (), (), (), ())
        high = SafetyAssessment(RiskLevel.HIGH, ("illness",), ("rest",), (), ())
        original = planner.generate(
            athlete_id="athlete-001",
            goal=goal,
            constraints=constraints,
            safety=low,
            start_date=date(2026, 10, 26),
        )

        adjusted = planner.adjust(original, safety=high, reason="recovery signal")

        self.assertEqual(original.version, 1)
        self.assertEqual(adjusted.version, 2)
        self.assertEqual(adjusted.previous_plan_id, original.plan_id)
        self.assertTrue(all(session.expected_load == 0 for session in adjusted.sessions))
        self.assertTrue(any(session.expected_load > 0 for session in original.sessions))

    def test_writeback_requires_explicit_confirmation(self) -> None:
        gate_type = getattr(workout_sink, "ConfirmationGatedWorkoutSink", None)
        error_type = getattr(workout_sink, "ConfirmationRequired", None)
        self.assertIsNotNone(gate_type)
        self.assertIsNotNone(error_type)
        gate = gate_type(lambda payload: "remote-42")

        preview = gate.preview({"name": "Easy run"})
        with self.assertRaises(error_type):
            gate.write(preview, confirmed=False)
        receipt = gate.write(preview, confirmed=True)

        self.assertEqual(receipt.remote_id, "remote-42")
        self.assertTrue(receipt.confirmed)

    def test_rollback_creates_a_new_version_and_preserves_both_histories(self) -> None:
        planner = service.TrainingPlanner()
        constraints = TrainingConstraints({0: 60, 2: 60}, frozenset(), 120, ("running",))
        goal = Goal("g1", "athlete-001", "race", "10K", 5, date(2027, 1, 1))
        low = SafetyAssessment(RiskLevel.LOW, (), (), (), ())
        high = SafetyAssessment(RiskLevel.HIGH, ("illness",), ("rest",), (), ())
        original = planner.generate(
            athlete_id="athlete-001",
            goal=goal,
            constraints=constraints,
            safety=low,
            start_date=date(2026, 10, 26),
        )
        adjusted = planner.adjust(original, safety=high, reason="recovery signal")

        rolled_back = planner.rollback(
            current=adjusted,
            target=original,
            reason="athlete rejected the adjustment",
        )

        self.assertEqual(rolled_back.version, 3)
        self.assertEqual(rolled_back.previous_plan_id, adjusted.plan_id)
        self.assertEqual(rolled_back.sessions, original.sessions)
        self.assertIn("rollback", rolled_back.change_reason)
        self.assertEqual(adjusted.version, 2)


if __name__ == "__main__":
    unittest.main()
