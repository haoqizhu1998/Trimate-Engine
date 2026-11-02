from __future__ import annotations

import unittest

from athlete_growth.application.nutrition import service
from athlete_growth.domain.nutrition import models


class NutritionPlanTests(unittest.TestCase):
    def test_race_plan_differs_from_short_easy_training(self) -> None:
        planner_type = getattr(service, "NutritionPlanner", None)
        athlete_type = getattr(models, "NutritionProfile", None)
        session_type = getattr(models, "FuelingContext", None)
        self.assertIsNotNone(planner_type)
        athlete = athlete_type(weight_kg=70, dietary_preferences=("vegetarian",))
        planner = planner_type()

        easy = planner.plan(
            athlete,
            session_type(duration_min=45, intensity="easy", is_race=False),
        )
        race = planner.plan(
            athlete,
            session_type(duration_min=180, intensity="race", is_race=True),
        )

        self.assertEqual(easy.carbohydrate_g_per_hour, (0, 0))
        self.assertGreaterEqual(race.carbohydrate_g_per_hour[0], 60)
        self.assertNotEqual(easy.pre_session, race.pre_session)
        self.assertIn("general", race.disclaimer.lower())

    def test_short_race_does_not_get_long_event_carbohydrate_target(self) -> None:
        athlete = models.NutritionProfile(weight_kg=70)
        plan = service.NutritionPlanner().plan(
            athlete,
            models.FuelingContext(duration_min=45, intensity="hard", is_race=True),
        )
        self.assertEqual(plan.carbohydrate_g_per_hour, (0, 0))

    def test_heat_does_not_raise_fixed_fluid_target_above_measured_sweat_rate(self) -> None:
        athlete = models.NutritionProfile(weight_kg=70, sweat_rate_ml_per_hour=450)
        plan = service.NutritionPlanner().plan(
            athlete,
            models.FuelingContext(
                duration_min=180, intensity="hard", is_race=True, temperature_c=30
            ),
        )
        self.assertEqual(plan.fluid_ml_per_hour, (450, 450))
        self.assertTrue(any("heat" in gap for gap in plan.data_gaps))


if __name__ == "__main__":
    unittest.main()
