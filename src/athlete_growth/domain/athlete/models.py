"""Athlete profile domain models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UnitSystem = Literal["metric", "statute"]


@dataclass(frozen=True, slots=True)
class AthleteProfile:
    """Identity and stable preferences for one athlete profile."""

    athlete_id: str
    display_name: str
    timezone_name: str
    unit_system: UnitSystem
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.display_name.strip():
            raise ValueError("display name must not be blank")
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {self.timezone_name}") from exc
        if self.unit_system not in ("metric", "statute"):
            raise ValueError(f"unknown unit system: {self.unit_system}")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
