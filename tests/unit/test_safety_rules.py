from __future__ import annotations

import unittest
from datetime import UTC, datetime

from athlete_growth.domain.records import CanonicalRecord
from athlete_growth.domain.safety import rules


class SafetyRulesTests(unittest.TestCase):
    def test_missing_check_in_is_unknown_not_low_risk(self) -> None:
        engine_type = getattr(rules, "SafetyRules", None)
        level_type = getattr(rules, "RiskLevel", None)
        self.assertIsNotNone(engine_type)
        self.assertIsNotNone(level_type)

        assessment = engine_type().assess(check_in=None, observations=[])

        self.assertEqual(assessment.level, level_type.UNKNOWN)
        self.assertIn("daily check-in", assessment.data_gaps)

    def test_illness_or_high_pain_escalates_before_training_advice(self) -> None:
        engine_type = getattr(rules, "SafetyRules", None)
        level_type = getattr(rules, "RiskLevel", None)
        self.assertIsNotNone(engine_type)
        check_in = CanonicalRecord.create(
            athlete_id="athlete-001",
            kind="check-in",
            occurred_at=datetime.now(UTC),
            data={"pain": 8, "illness": True, "fatigue": 5, "soreness": 5},
        )

        assessment = engine_type().assess(check_in=check_in, observations=[])

        self.assertEqual(assessment.level, level_type.HIGH)
        self.assertTrue(any("professional" in action.lower() for action in assessment.actions))
        self.assertIn("not medical", assessment.disclaimer.lower())

    def test_load_ratio_alone_does_not_classify_injury_risk(self) -> None:
        engine_type = getattr(rules, "SafetyRules", None)
        level_type = getattr(rules, "RiskLevel", None)
        self.assertIsNotNone(engine_type)
        check_in = CanonicalRecord.create(
            athlete_id="athlete-001",
            kind="check-in",
            occurred_at=datetime.now(UTC),
            data={"pain": 0, "illness": False, "fatigue": 2, "soreness": 2},
        )

        assessment = engine_type().assess(check_in=check_in, observations=[])

        self.assertEqual(assessment.level, level_type.LOW)
        self.assertNotIn("load", " ".join(assessment.reasons).lower())


if __name__ == "__main__":
    unittest.main()
