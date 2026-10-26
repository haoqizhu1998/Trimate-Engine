"""Non-diagnostic recovery state models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import IntEnum
from typing import Literal


class ReturnStage(IntEnum):
    PROTECT = 0
    RESTORE = 1
    RELOAD = 2
    RETURN = 3
    UNRESTRICTED = 4


@dataclass(frozen=True, slots=True)
class InjuryStatus:
    injury_id: str
    body_area: str
    started_on: date
    status: Literal["active", "recovering", "cleared"]
    notes: str
    professional_clearance: bool = False


@dataclass(frozen=True, slots=True)
class ReturnToTrainingPlan:
    stage: ReturnStage
    allowed_load_pct: int
    actions: tuple[str, ...]
    red_flags: tuple[str, ...]
    requires_professional_review: bool
    disclaimer: str
