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


if __name__ == "__main__":
    unittest.main()
