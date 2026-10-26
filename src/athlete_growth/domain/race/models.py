"""Provenance-aware race events and offline strategies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class RaceEvent:
    event_id: str
    name: str
    event_date: date
    distance_km: float
    elevation_gain_m: float
    location: str
    source_url: str
    source_updated_at: datetime
    license: str

    def __post_init__(self) -> None:
        if self.distance_km <= 0:
            raise ValueError("race distance must be positive")
        if self.source_updated_at.tzinfo is None:
            raise ValueError("source_updated_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class RaceStrategy:
    event: RaceEvent
    target_finish_min: float
    target_pace_min_per_km: tuple[float, float]
    pacing: tuple[str, ...]
    fueling: tuple[str, ...]
    equipment: tuple[str, ...]
    logistics: tuple[str, ...]
    risks: tuple[str, ...]
    evidence: tuple[str, ...]
    data_gaps: tuple[str, ...]
    disclaimer: str
