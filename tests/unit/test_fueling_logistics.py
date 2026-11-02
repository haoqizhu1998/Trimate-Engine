from __future__ import annotations

import unittest

from athlete_growth.application.nutrition.logistics import preview_fueling_logistics


class FuelingLogisticsTests(unittest.TestCase):
    def test_declared_plan_reports_access_shortfall(self) -> None:
        result = preview_fueling_logistics(
            duration_min=180,
            declared_tested_carbs_g_per_hour=60,
            carbs_per_serving_g=30,
            available_servings=8,
            opportunity_times_min=[30, 60, 90, 120],
        )
        self.assertEqual(result["servings_needed"], 6)
        self.assertEqual(result["accessible_servings"], 4)
        self.assertEqual(result["shortfall_g"], 60)
        self.assertFalse(result["meets_declared_target"])
        self.assertFalse(result["saved"])

    def test_duplicate_or_late_opportunities_rejected(self) -> None:
        values = dict(
            duration_min=120,
            declared_tested_carbs_g_per_hour=60,
            carbs_per_serving_g=30,
            available_servings=4,
        )
        for times in ([30, 30], [30, 121], [60, 30]):
            with self.assertRaisesRegex(ValueError, "opportunities"):
                preview_fueling_logistics(**values, opportunity_times_min=times)


if __name__ == "__main__":
    unittest.main()
