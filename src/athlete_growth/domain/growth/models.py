"""Milestones, feedback and correlation-only response insights."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class GrowthMilestone:
    milestone_id: str
    athlete_id: str
    occurred_at: datetime
    title: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResponseInsight:
    stimulus_name: str
    outcome_name: str
    correlation: float
    sample_size: int
    statement: str


@dataclass(frozen=True, slots=True)
class UserFeedback:
    recommendation_id: str
    accepted: bool
    outcome: str | None
    recorded_at: datetime
