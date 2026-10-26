from __future__ import annotations

import unittest
from datetime import UTC, datetime

from athlete_growth.domain.athlete import models


class AthleteProfileTests(unittest.TestCase):
    def test_creates_profile_with_valid_timezone_and_units(self) -> None:
        profile_type = getattr(models, "AthleteProfile", None)
        self.assertIsNotNone(profile_type, "AthleteProfile must exist")

        profile = profile_type(
            athlete_id="athlete-001",
            display_name="Demo Athlete",
            timezone_name="Asia/Shanghai",
            unit_system="metric",
            created_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
        )

        self.assertEqual(profile.athlete_id, "athlete-001")
        self.assertEqual(profile.display_name, "Demo Athlete")
        self.assertEqual(profile.timezone_name, "Asia/Shanghai")
        self.assertEqual(profile.unit_system, "metric")

    def test_rejects_blank_display_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "display name"):
            models.AthleteProfile(
                athlete_id="athlete-001",
                display_name="   ",
                timezone_name="Asia/Shanghai",
                unit_system="metric",
                created_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
            )

    def test_rejects_unknown_timezone(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone"):
            models.AthleteProfile(
                athlete_id="athlete-001",
                display_name="Demo Athlete",
                timezone_name="Moon/Sea_of_Tranquility",
                unit_system="metric",
                created_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
            )

    def test_rejects_unknown_unit_system(self) -> None:
        with self.assertRaisesRegex(ValueError, "unit system"):
            models.AthleteProfile(
                athlete_id="athlete-001",
                display_name="Demo Athlete",
                timezone_name="Asia/Shanghai",
                unit_system="cubits",
                created_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
            )

    def test_rejects_naive_creation_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            models.AthleteProfile(
                athlete_id="athlete-001",
                display_name="Demo Athlete",
                timezone_name="Asia/Shanghai",
                unit_system="metric",
                created_at=datetime(2026, 10, 26, 8, 0),
            )


if __name__ == "__main__":
    unittest.main()
