"""SQLite-backed athlete profile persistence."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import cast

from athlete_growth.domain.athlete import AthleteProfile, UnitSystem


class SQLiteAthleteProfileRepository:
    """Persist athlete profiles in a private, profile-local SQLite database."""

    def __init__(self, database_file: Path) -> None:
        self._database_file = database_file
        self._database_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._database_file.parent, 0o700)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_file)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY
                );

                CREATE TABLE IF NOT EXISTS athlete_profiles (
                    athlete_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    timezone_name TEXT NOT NULL,
                    unit_system TEXT NOT NULL
                        CHECK (unit_system IN ('metric', 'statute')),
                    created_at TEXT NOT NULL
                );

                INSERT OR IGNORE INTO schema_migrations(version) VALUES (1);
                """
            )
        os.chmod(self._database_file, 0o600)

    def save(self, profile: AthleteProfile) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO athlete_profiles (
                    athlete_id,
                    display_name,
                    timezone_name,
                    unit_system,
                    created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(athlete_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    timezone_name = excluded.timezone_name,
                    unit_system = excluded.unit_system,
                    created_at = excluded.created_at
                """,
                (
                    profile.athlete_id,
                    profile.display_name,
                    profile.timezone_name,
                    profile.unit_system,
                    profile.created_at.isoformat(),
                ),
            )

    def get_by_id(self, athlete_id: str) -> AthleteProfile | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT athlete_id, display_name, timezone_name, unit_system, created_at
                FROM athlete_profiles
                WHERE athlete_id = ?
                """,
                (athlete_id,),
            ).fetchone()
        if row is None:
            return None
        return AthleteProfile(
            athlete_id=row["athlete_id"],
            display_name=row["display_name"],
            timezone_name=row["timezone_name"],
            unit_system=cast(UnitSystem, row["unit_system"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
