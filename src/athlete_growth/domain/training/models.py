"""Versioned plans and session state transitions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Literal

SessionStatus = Literal[
    "draft",
    "approved",
    "scheduled",
    "completed",
    "partial",
    "skipped",
    "substituted",
    "cancelled",
]


@dataclass(frozen=True, slots=True)
class TrainingConstraints:
    available_minutes_by_weekday: dict[int, int]
    unavailable_weekdays: frozenset[int]
    max_weekly_minutes: int
    allowed_sports: tuple[str, ...]
    health_constraints: tuple[str, ...] = ()
    equipment: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.max_weekly_minutes < 0:
            raise ValueError("max_weekly_minutes must not be negative")
        if any(day < 0 or day > 6 for day in self.available_minutes_by_weekday):
            raise ValueError("weekdays must be between 0 and 6")
        if any(minutes < 0 for minutes in self.available_minutes_by_weekday.values()):
            raise ValueError("available minutes must not be negative")


@dataclass(frozen=True, slots=True)
class PlannedSession:
    session_id: str
    session_date: date
    sport: str
    duration_min: int
    intensity: str
    objective: str
    expected_load: float
    status: SessionStatus = "draft"
    outcome_note: str | None = None

    def transition(
        self, status: SessionStatus, *, outcome_note: str | None = None
    ) -> PlannedSession:
        allowed: dict[str, set[str]] = {
            "draft": {"approved", "cancelled"},
            "approved": {"scheduled", "cancelled"},
            "scheduled": {
                "completed",
                "partial",
                "skipped",
                "substituted",
                "cancelled",
            },
        }
        if status not in allowed.get(self.status, set()):
            raise ValueError(f"invalid session transition: {self.status} -> {status}")
        return replace(self, status=status, outcome_note=outcome_note)


@dataclass(frozen=True, slots=True)
class TrainingPlan:
    plan_id: str
    athlete_id: str
    goal_id: str
    version: int
    created_at: datetime
    sessions: tuple[PlannedSession, ...]
    status: str = "draft"
    change_reason: str = "initial plan"
    previous_plan_id: str | None = None

    def __post_init__(self) -> None:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if self.version < 1:
            raise ValueError("plan version must be positive")
