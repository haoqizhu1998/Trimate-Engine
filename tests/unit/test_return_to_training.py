from __future__ import annotations

import unittest
from datetime import date

from athlete_growth.application.recovery import service
from athlete_growth.domain.recovery import models


class ReturnToTrainingTests(unittest.TestCase):
    def test_high_pain_pauses_progression_and_escalates(self) -> None:
        service_type = getattr(service, "ReturnToTrainingService", None)
        injury_type = getattr(models, "InjuryStatus", None)
        self.assertIsNotNone(service_type)
        self.assertIsNotNone(injury_type)
        injury = injury_type(
            injury_id="i1",
            body_area="knee",
            started_on=date(2026, 10, 20),
            status="recovering",
            notes="user reported",
        )

        plan = service_type().assess(
            injury=injury,
            pain=8,
            swelling=True,
            normal_daily_function=False,
            stable_days=0,
        )

        self.assertEqual(plan.allowed_load_pct, 0)
        self.assertTrue(plan.requires_professional_review)
        self.assertIn("not medical", plan.disclaimer.lower())

    def test_progression_advances_only_one_stage_at_a_time(self) -> None:
        service_type = getattr(service, "ReturnToTrainingService", None)
        stage_type = getattr(models, "ReturnStage", None)
        self.assertIsNotNone(service_type)

        next_stage = service_type().progress(
            current_stage=stage_type.RESTORE,
            pain=1,
            swelling=False,
            normal_daily_function=True,
            stable_days=7,
        )

        self.assertEqual(next_stage, stage_type.RELOAD)


if __name__ == "__main__":
    unittest.main()
