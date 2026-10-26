from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from athlete_growth.adapters.storage import sqlite_profiles
from athlete_growth.domain.athlete import AthleteProfile


class SQLiteAthleteProfileRepositoryTests(unittest.TestCase):
    def test_round_trips_profile_and_secures_database_file(self) -> None:
        repository_type = getattr(sqlite_profiles, "SQLiteAthleteProfileRepository", None)
        self.assertIsNotNone(repository_type, "SQLiteAthleteProfileRepository must exist")
        profile = AthleteProfile(
            athlete_id="athlete-001",
            display_name="Demo Athlete",
            timezone_name="Asia/Shanghai",
            unit_system="metric",
            created_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
        )

        with TemporaryDirectory() as directory:
            database_file = Path(directory) / "db" / "athlete.sqlite3"
            repository = repository_type(database_file)
            repository.save(profile)

            self.assertEqual(repository.get_by_id(profile.athlete_id), profile)
            self.assertEqual(database_file.stat().st_mode & 0o777, 0o600)

    def test_returns_none_for_unknown_profile(self) -> None:
        repository_type = getattr(sqlite_profiles, "SQLiteAthleteProfileRepository", None)
        self.assertIsNotNone(repository_type, "SQLiteAthleteProfileRepository must exist")

        with TemporaryDirectory() as directory:
            repository = repository_type(Path(directory) / "athlete.sqlite3")

            self.assertIsNone(repository.get_by_id("missing"))


if __name__ == "__main__":
    unittest.main()
