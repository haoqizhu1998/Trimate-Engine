from __future__ import annotations

import unittest
from datetime import date

from athlete_growth.application.race.segments import preview_race_segments


class RaceSegmentTests(unittest.TestCase):
    def test_triathlon_segments_use_correct_units_and_transitions(self) -> None:
        result = preview_race_segments(
            event_name="Synthetic Olympic",
            event_date=date(2026, 12, 1),
            sport="triathlon",
            segments=[
                {"sport": "swimming", "distance_km": 1.5, "target_duration_min": 30},
                {"sport": "transition", "target_duration_min": 3},
                {"sport": "cycling", "distance_km": 40, "target_duration_min": 80},
                {"sport": "transition", "target_duration_min": 2},
                {"sport": "running", "distance_km": 10, "target_duration_min": 50},
            ],
        )
        self.assertEqual(result["total_target_min"], 165)
        self.assertEqual(result["segments"][0]["derived_target"], {"pace_min_per_100m": 2})
        self.assertEqual(result["segments"][2]["derived_target"], {"speed_km_per_hour": 30})
        self.assertEqual(result["segments"][4]["derived_target"], {"pace_min_per_km": 5})
        self.assertFalse(result["saved"])

    def test_mismatched_segments_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "do not match"):
            preview_race_segments(
                event_name="Synthetic",
                event_date=date(2026, 12, 1),
                sport="triathlon",
                segments=[
                    {"sport": "running", "distance_km": 10, "target_duration_min": 50}
                ],
            )


if __name__ == "__main__":
    unittest.main()
