"""Constraint-safe training plan generation and adjustment."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta

from athlete_growth.adapters.storage.sqlite_records import SQLiteRecordStore
from athlete_growth.domain.goals import Goal
from athlete_growth.domain.records import CanonicalRecord
from athlete_growth.domain.safety import RiskLevel, SafetyAssessment
from athlete_growth.domain.training import (
    PlannedSession,
    TrainingConstraints,
    TrainingPlan,
)


class TrainingPlanner:
    algorithm_version = "training-planner/1.0"

    def generate(
        self,
        *,
        athlete_id: str,
        goal: Goal,
        constraints: TrainingConstraints,
        safety: SafetyAssessment,
        start_date: date,
    ) -> TrainingPlan:
        remaining = constraints.max_weekly_minutes
        sessions: list[PlannedSession] = []
        sport = constraints.allowed_sports[0] if constraints.allowed_sports else "general"
        for offset in range(7):
            session_date = start_date + timedelta(days=offset)
            weekday = session_date.weekday()
            if weekday in constraints.unavailable_weekdays:
                continue
            available = constraints.available_minutes_by_weekday.get(weekday, 0)
            if available <= 0 or remaining <= 0:
                continue
            duration = min(available, remaining)
            intensity, load_factor, objective = self._prescription(
                safety.level, len(sessions), goal
            )
            load = round(duration * load_factor, 1)
            session_id = self._digest(
                athlete_id, goal.goal_id, session_date.isoformat(), str(len(sessions))
            )
            sessions.append(
                PlannedSession(
                    session_id,
                    session_date,
                    sport,
                    duration,
                    intensity,
                    objective,
                    load,
                )
            )
            remaining -= duration
        plan_id = self._digest(
            athlete_id,
            goal.goal_id,
            start_date.isoformat(),
            "1",
            safety.level.value,
        )
        return TrainingPlan(
            plan_id,
            athlete_id,
            goal.goal_id,
            1,
            datetime.now(UTC),
            tuple(sessions),
        )

    def adjust(self, plan: TrainingPlan, *, safety: SafetyAssessment, reason: str) -> TrainingPlan:
        sessions: list[PlannedSession] = []
        for session in plan.sessions:
            if safety.level == RiskLevel.HIGH:
                sessions.append(
                    replace(
                        session,
                        intensity="rest",
                        objective="Recovery and reassessment; no training load prescribed.",
                        expected_load=0,
                    )
                )
            elif safety.level in {RiskLevel.MODERATE, RiskLevel.UNKNOWN}:
                sessions.append(
                    replace(
                        session,
                        duration_min=max(0, session.duration_min // 2),
                        intensity="easy",
                        objective="Reduced session within current recovery constraints.",
                        expected_load=round(session.expected_load * 0.4, 1),
                    )
                )
            else:
                sessions.append(session)
        version = plan.version + 1
        plan_id = self._digest(plan.plan_id, str(version), reason, safety.level.value)
        return TrainingPlan(
            plan_id,
            plan.athlete_id,
            plan.goal_id,
            version,
            datetime.now(UTC),
            tuple(sessions),
            status=plan.status,
            change_reason=reason,
            previous_plan_id=plan.plan_id,
        )

    def rollback(
        self,
        *,
        current: TrainingPlan,
        target: TrainingPlan,
        reason: str,
    ) -> TrainingPlan:
        """Restore a prior prescription as a new immutable plan version."""
        if current.athlete_id != target.athlete_id or current.goal_id != target.goal_id:
            raise ValueError("rollback plans must belong to the same athlete and goal")
        version = current.version + 1
        change_reason = f"rollback to version {target.version}: {reason}"
        plan_id = self._digest(current.plan_id, target.plan_id, str(version), change_reason)
        return TrainingPlan(
            plan_id,
            current.athlete_id,
            current.goal_id,
            version,
            datetime.now(UTC),
            target.sessions,
            status=current.status,
            change_reason=change_reason,
            previous_plan_id=current.plan_id,
        )

    def save(self, plan: TrainingPlan, store: SQLiteRecordStore) -> CanonicalRecord:
        record = CanonicalRecord.create(
            athlete_id=plan.athlete_id,
            kind="training-plan",
            occurred_at=plan.created_at,
            data=json.loads(json.dumps(asdict(plan), default=str)),
            supersedes_id=plan.previous_plan_id,
        )
        # Plan versions are append-only. ``previous_plan_id`` is a domain identifier,
        # not a canonical-record identifier, and remains in the serialized plan for
        # history traversal and rollback.
        store.save(record)
        return record

    @staticmethod
    def _prescription(risk: RiskLevel, index: int, goal: Goal) -> tuple[str, float, str]:
        if risk == RiskLevel.HIGH:
            return "rest", 0.0, "Recovery and reassessment; no training load prescribed."
        if risk in {RiskLevel.MODERATE, RiskLevel.UNKNOWN}:
            return "easy", 1.0, "Maintain movement without increasing recovery risk."
        if index == 1 and goal.goal_type in {"sport", "race"}:
            return "quality", 3.0, f"Develop capacity relevant to {goal.title}."
        return "easy", 1.5, f"Build consistent aerobic support for {goal.title}."

    @staticmethod
    def _digest(*parts: str) -> str:
        return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:32]
