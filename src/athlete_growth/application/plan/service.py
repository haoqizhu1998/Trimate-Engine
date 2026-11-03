"""Constraint-safe training plan generation and adjustment."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta

from athlete_growth.domain.goals import Goal
from athlete_growth.domain.records import CanonicalRecord
from athlete_growth.domain.safety import RiskLevel, SafetyAssessment
from athlete_growth.domain.training import (
    PlannedSession,
    TrainingConstraints,
    TrainingPlan,
)
from athlete_growth.ports.repositories import CanonicalRecordRepository


class TrainingPlanner:
    algorithm_version = "training-planner/1.3"

    def generate(
        self,
        *,
        athlete_id: str,
        goal: Goal,
        constraints: TrainingConstraints,
        safety: SafetyAssessment,
        start_date: date,
        recent_sessions: tuple[tuple[str, int, date], ...] = (),
    ) -> TrainingPlan:
        if safety.level == RiskLevel.HIGH:
            return TrainingPlan(
                self._digest(athlete_id, goal.goal_id, start_date.isoformat(), "blocked"),
                athlete_id,
                goal.goal_id,
                1,
                datetime.now(UTC),
                (),
                change_reason="high-risk safety assessment; no training prescribed",
            )
        recent = [item for item in recent_sessions if 0 < (start_date - item[2]).days <= 28]
        observed_longest = {
            sport: max(
                (minutes for logged_sport, minutes, _ in recent if logged_sport == sport), default=0
            )
            for sport in constraints.allowed_sports
        }
        # The user-provided maximum is a ceiling, never a target to fill.
        weekly_history = [
            sum(
                minutes
                for _, minutes, day in recent
                if start_date - timedelta(days=7 * (week + 1))
                <= day
                < start_date - timedelta(days=7 * week)
            )
            for week in range(4)
        ]
        observed_ceiling = max(weekly_history, default=0)
        available_days = sum(
            1
            for offset in range(7)
            if (start_date + timedelta(days=offset)).weekday()
            not in constraints.unavailable_weekdays
            and constraints.available_minutes_by_weekday.get(
                (start_date + timedelta(days=offset)).weekday(), 0
            )
            > 0
        )
        provisional_ceiling = 45 * available_days
        remaining = min(constraints.max_weekly_minutes, observed_ceiling or provisional_ceiling)
        sessions: list[PlannedSession] = []
        sports = constraints.allowed_sports
        focus = constraints.focus_sport or sports[0]
        available_dates = [
            start_date + timedelta(days=offset)
            for offset in range(7)
            if (start_date + timedelta(days=offset)).weekday()
            not in constraints.unavailable_weekdays
            and constraints.available_minutes_by_weekday.get(
                (start_date + timedelta(days=offset)).weekday(), 0
            )
            > 0
        ]
        long_date = next(
            (day for day in available_dates if day.weekday() == constraints.preferred_long_weekday),
            None,
        )
        quality_date = next(
            (
                day
                for day in available_dates
                if day.weekday() == constraints.preferred_quality_weekday
            ),
            None,
        )
        can_key = safety.level == RiskLevel.LOW and observed_longest.get(focus, 0) > 0
        if not can_key or (
            long_date and quality_date and abs((long_date - quality_date).days) <= 1
        ):
            quality_date = None
        if not can_key:
            long_date = None
        key_dates = {day for day in (quality_date, long_date) if day is not None}
        for offset in range(7):
            session_date = start_date + timedelta(days=offset)
            weekday = session_date.weekday()
            if weekday in constraints.unavailable_weekdays:
                continue
            available = constraints.available_minutes_by_weekday.get(weekday, 0)
            if available <= 0 or remaining <= 0:
                continue
            session_type = (
                "quality"
                if session_date == quality_date
                else "long"
                if session_date == long_date
                else "recovery"
                if any(
                    (session_date - item.session_date).days == 1
                    and item.session_type in {"quality", "long"}
                    for item in sessions
                )
                else "aerobic"
            )
            sport = (
                focus
                if session_type in {"quality", "long"} or constraints.aerobic_mixing is False
                else sports[len(sessions) % len(sports)]
            )
            # Do not prescribe an unobserved long session from availability alone.
            exposure_cap = observed_longest.get(sport, 0) or 45
            reserve = sum(
                min(
                    constraints.available_minutes_by_weekday.get(day.weekday(), 0),
                    observed_longest.get(focus, 0),
                )
                // 2
                for day in key_dates
                if day > session_date
            )
            duration = min(available, max(0, remaining - reserve), exposure_cap)
            if session_type == "recovery":
                prior_key = next(
                    (
                        item
                        for item in reversed(sessions)
                        if (session_date - item.session_date).days == 1
                        and item.session_type in {"quality", "long"}
                    ),
                    None,
                )
                if prior_key is not None:
                    duration = min(duration, max(1, prior_key.duration_min // 2))
            if duration <= 0:
                continue
            intensity = (
                "quality"
                if session_type == "quality"
                else "easy"
            )
            objective = (
                f"Goal-specific session for {goal.title}; review current effort and recovery."
                if session_type == "quality"
                else f"Observed-duration endurance session for {goal.title}."
                if session_type == "long"
                else f"Easy cross-training after a key session for {goal.title}."
                if session_type == "recovery"
                else f"Sustainable aerobic support for {goal.title}."
            )
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
                    float(duration),
                    session_type=session_type,
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
                        duration_min=0,
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

    def save(self, plan: TrainingPlan, store: CanonicalRecordRepository) -> CanonicalRecord:
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
