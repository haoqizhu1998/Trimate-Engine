"""Goal domain."""

from athlete_growth.domain.goals.models import Goal, GoalConflict, GoalType, detect_conflicts

__all__ = ["Goal", "GoalConflict", "GoalType", "detect_conflicts"]
