"""Athlete profile application use cases."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from athlete_growth.domain.athlete import AthleteProfile, UnitSystem
from athlete_growth.ports import AthleteProfileRepository, ProfileRegistryPort

RepositoryFactory = Callable[[str], AthleteProfileRepository]


def _new_athlete_id() -> str:
    return str(uuid4())


def _utc_now() -> datetime:
    return datetime.now(UTC)


def initialize_profile(
    *,
    display_name: str,
    timezone_name: str,
    unit_system: UnitSystem,
    repository_factory: RepositoryFactory,
    registry: ProfileRegistryPort,
    athlete_id_factory: Callable[[], str] = _new_athlete_id,
    clock: Callable[[], datetime] = _utc_now,
) -> AthleteProfile:
    """Create one profile and persist it through application ports."""

    athlete_id = athlete_id_factory()
    profile = AthleteProfile(
        athlete_id=athlete_id,
        display_name=display_name,
        timezone_name=timezone_name,
        unit_system=unit_system,
        created_at=clock(),
    )
    repository = repository_factory(athlete_id)
    repository.save(profile)
    registry.register(athlete_id)
    return profile
