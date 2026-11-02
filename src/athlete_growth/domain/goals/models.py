"""Sport, body and race goals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

GoalType = Literal["sport", "body", "race"]


@dataclass(frozen=True, slots=True)
class Goal:
    goal_id: str
    athlete_id: str
    goal_type: GoalType
    title: str
    priority: int
    deadline: date | None
    target_metric: str | None = None
    target_value: float | None = None
    status: str = "active"

    def __post_init__(self) -> None:
        if self.goal_type not in ("sport", "body", "race"):
            raise ValueError("goal type must be sport, body, or race")
        if not 1 <= self.priority <= 5:
            raise ValueError("goal priority must be between 1 and 5")
        if not self.title.strip():
            raise ValueError("goal title must not be blank")


@dataclass(frozen=True, slots=True)
class GoalConflict:
    goal_ids: tuple[str, str]
    reason: str


def detect_conflicts(goals: list[Goal]) -> list[GoalConflict]:
    # Priority is a rank: 1 is the most important goal.
    active = [goal for goal in goals if goal.status == "active" and goal.priority <= 2]
    conflicts: list[GoalConflict] = []
    for index, left in enumerate(active):
        for right in active[index + 1 :]:
            if left.deadline is None or right.deadline is None:
                continue
            if abs((left.deadline - right.deadline).days) <= 14:
                conflicts.append(
                    GoalConflict(
                        (left.goal_id, right.goal_id),
                        "high-priority goals have competing deadlines within 14 days",
                    )
                )
    return conflicts
