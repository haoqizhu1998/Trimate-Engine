"""Persistence ports."""

from __future__ import annotations

from typing import Protocol

from athlete_growth.domain.athlete import AthleteProfile


class AthleteProfileRepository(Protocol):
    def save(self, profile: AthleteProfile) -> None: ...

    def get_by_id(self, athlete_id: str) -> AthleteProfile | None: ...


class ProfileRegistryPort(Protocol):
    def register(self, athlete_id: str) -> None: ...

    def default_profile_id(self) -> str | None: ...

    def profile_ids(self) -> tuple[str, ...]: ...
