from __future__ import annotations

import unittest
from datetime import date

from athlete_growth.domain.goals import models


class GoalTests(unittest.TestCase):
    def test_detects_competing_high_priority_deadlines(self) -> None:
        goal_type = getattr(models, "Goal", None)
        detector = getattr(models, "detect_conflicts", None)
        self.assertIsNotNone(goal_type)
        self.assertIsNotNone(detector)
        goals = [
            goal_type("g1", "athlete-001", "race", "Marathon", 5, date(2027, 1, 1)),
            goal_type("g2", "athlete-001", "body", "Lose weight", 5, date(2027, 1, 8)),
        ]

        conflicts = detector(goals)

        self.assertEqual(len(conflicts), 1)
        self.assertEqual(set(conflicts[0].goal_ids), {"g1", "g2"})

    def test_rejects_invalid_priority(self) -> None:
        goal_type = getattr(models, "Goal", None)
        self.assertIsNotNone(goal_type)
        with self.assertRaisesRegex(ValueError, "priority"):
            goal_type("g1", "athlete-001", "sport", "Run", 0, None)


if __name__ == "__main__":
    unittest.main()
