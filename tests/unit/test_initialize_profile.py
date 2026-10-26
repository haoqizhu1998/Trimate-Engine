from __future__ import annotations

import unittest
from datetime import UTC, datetime

from athlete_growth.application import profiles


class RecordingRepository:
    def __init__(self) -> None:
        self.saved = []

    def save(self, profile) -> None:
        self.saved.append(profile)


class RecordingRegistry:
    def __init__(self) -> None:
        self.registered = []

    def register(self, athlete_id: str) -> None:
        self.registered.append(athlete_id)


class InitializeProfileTests(unittest.TestCase):
    def test_creates_persists_and_registers_profile(self) -> None:
        initialize_profile = getattr(profiles, "initialize_profile", None)
        self.assertIsNotNone(initialize_profile, "initialize_profile must exist")
        repository = RecordingRepository()
        registry = RecordingRegistry()
        repository_ids = []

        def repository_factory(athlete_id: str) -> RecordingRepository:
            repository_ids.append(athlete_id)
            return repository

        created_at = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
        profile = initialize_profile(
            display_name="Demo Athlete",
            timezone_name="Asia/Shanghai",
            unit_system="metric",
            repository_factory=repository_factory,
            registry=registry,
            athlete_id_factory=lambda: "athlete-001",
            clock=lambda: created_at,
        )

        self.assertEqual(profile.athlete_id, "athlete-001")
        self.assertEqual(profile.created_at, created_at)
        self.assertEqual(repository_ids, ["athlete-001"])
        self.assertEqual(repository.saved, [profile])
        self.assertEqual(registry.registered, ["athlete-001"])


if __name__ == "__main__":
    unittest.main()
