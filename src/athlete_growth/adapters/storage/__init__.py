"""Local storage adapters."""

from athlete_growth.adapters.storage.profile_registry import ProfileRegistry
from athlete_growth.adapters.storage.sqlite_automation import SQLiteAutomationRepository
from athlete_growth.adapters.storage.sqlite_profiles import (
    SQLiteAthleteProfileRepository,
)
from athlete_growth.adapters.storage.sqlite_records import SQLiteRecordStore
from athlete_growth.adapters.storage.vault import VaultLayout

__all__ = [
    "ProfileRegistry",
    "SQLiteAthleteProfileRepository",
    "SQLiteAutomationRepository",
    "SQLiteRecordStore",
    "VaultLayout",
]
