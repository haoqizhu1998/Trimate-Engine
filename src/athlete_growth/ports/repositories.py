"""Persistence ports."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from athlete_growth.domain.athlete import AthleteProfile
from athlete_growth.domain.records import CanonicalRecord


class AthleteProfileRepository(Protocol):
    def save(self, profile: AthleteProfile) -> None: ...

    def get_by_id(self, athlete_id: str) -> AthleteProfile | None: ...


class ProfileRegistryPort(Protocol):
    def register(self, athlete_id: str) -> None: ...

    def default_profile_id(self) -> str | None: ...

    def profile_ids(self) -> tuple[str, ...]: ...


class CanonicalRecordRepository(Protocol):
    def save(self, record: CanonicalRecord) -> bool: ...

    def get(self, record_id: str) -> CanonicalRecord | None: ...

    def list_current(
        self,
        athlete_id: str,
        *,
        kind: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[CanonicalRecord]: ...

    def list_current_ids(self, athlete_id: str) -> tuple[str, ...]: ...

    def replace_singleton(
        self,
        athlete_id: str,
        kind: str,
        expected_record_id: str | None,
        replacement: CanonicalRecord,
    ) -> CanonicalRecord: ...

    def list_by_ids(
        self,
        athlete_id: str,
        record_ids: tuple[str, ...],
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[CanonicalRecord]: ...
