"""Constraint-safe training plan generation and adjustment."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from math import ceil
from statistics import median

from athlete_growth.domain.goals import Goal
from athlete_growth.domain.records import CanonicalRecord
from athlete_growth.domain.safety import RiskLevel, SafetyAssessment
from athlete_growth.domain.training import (
    PlannedSession,
    TrainingConstraints,
    TrainingPlan,
)
from athlete_growth.ports.repositories import CanonicalRecordRepository


@dataclass(frozen=True, slots=True)
class VolumeDecision:
    target_minutes: int
    mode: str
    observed_week_minutes: tuple[int, int, int, int]
    typical_prior_minutes: int | None
    session_limit: int


class TrainingPlanner:
    algorithm_version = "training-planner/1.5"

    @staticmethod
    def volume_decision(
        *,
        start_date: date,
        recent_sessions: tuple[tuple[str, int, date], ...],
        constraints: TrainingConstraints,
        safety: SafetyAssessment,
    ) -> VolumeDecision:
        """Use sustained observed training, not one isolated week or an old peak."""
        weekly = [0, 0, 0, 0]
        session_counts = [0, 0, 0, 0]
        for sport, minutes, day in recent_sessions:
            age = (start_date - day).days
            if sport in constraints.allowed_sports and minutes > 0 and 1 <= age <= 28:
                index = (age - 1) // 7
                weekly[index] += minutes
                session_counts[index] += 1
        available_days = sum(
            1 for offset in range(7)
            if (day := start_date + timedelta(days=offset)).weekday()
            not in constraints.unavailable_weekdays
            and constraints.available_minutes_by_weekday.get(day.weekday(), 0) > 0
        )
        latest = weekly[0]
        active_prior = [value for value in weekly[1:] if value > 0]
        typical = round(median(active_prior)) if len(active_prior) >= 2 else None
        multisport_slots = (
            len(constraints.allowed_sports) + 1
            if len(constraints.allowed_sports) > 1
            and latest >= 20 * len(constraints.allowed_sports)
            else 0
        )
        if latest == 0:
            mode = "restart_or_unknown"
            target = 30 * min(available_days, max(2, min(3, len(constraints.allowed_sports))))
            session_limit = min(available_days, max(2, min(3, len(constraints.allowed_sports))))
        else:
            target = latest
            mode = "recent_hold"
            session_limit = min(available_days, max(1, session_counts[0], multisport_slots))
            stable_prior = (
                typical is not None
                and weekly[1] > 0 and weekly[2] > 0
                and min(active_prior) >= 0.6 * max(active_prior)
            )
            if safety.level != RiskLevel.LOW:
                mode = "safety_hold"
            elif stable_prior and typical is not None and session_counts[0] >= 2:
                if latest > 1.25 * typical:
                    mode = "recent_spike_hold"
                elif latest >= 0.8 * typical:
                    mode = "progressive_build"
                    target = latest + min(30, max(10, round(typical * 0.08)))
                    session_limit = min(
                        available_days, max(max(session_counts) + 1, multisport_slots)
                    )
                elif latest >= 0.5 * typical:
                    mode = "return_to_baseline"
                    target = round((latest + typical) / 2)
                    session_limit = min(
                        available_days, max(max(session_counts), multisport_slots)
                    )
        return VolumeDecision(
            target_minutes=min(constraints.max_weekly_minutes, target),
            mode=mode,
            observed_week_minutes=(weekly[0], weekly[1], weekly[2], weekly[3]),
            typical_prior_minutes=typical,
            session_limit=session_limit,
        )

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
        recent = [
            item for item in recent_sessions
            if item[0] in constraints.allowed_sports
            and item[1] > 0
            and 0 < (start_date - item[2]).days <= 28
        ]
        latest_week = [item for item in recent if (start_date - item[2]).days <= 7]
        latest_sports = {sport for sport, _, _ in latest_week}
        recent_exposure = [item for item in recent if (start_date - item[2]).days <= 14]
        observed_longest = {
            sport: max(
                (minutes for logged_sport, minutes, _ in recent_exposure
                 if logged_sport == sport), default=0
            )
            for sport in constraints.allowed_sports
        }
        older_longest = {
            sport: max(
                (minutes for logged_sport, minutes, _ in recent if logged_sport == sport),
                default=0,
            )
            for sport in constraints.allowed_sports
        }
        budget = self.volume_decision(
            start_date=start_date,
            recent_sessions=recent_sessions,
            constraints=constraints,
            safety=safety,
        )
        sparse_baseline = not latest_week
        remaining = budget.target_minutes
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
        can_key = safety.level == RiskLevel.LOW and any(
            sport == focus for sport, _, _ in latest_week
        )
        if not can_key or (
            long_date and quality_date and abs((long_date - quality_date).days) <= 1
        ):
            quality_date = None
        if not can_key:
            long_date = None
        key_dates = {day for day in (quality_date, long_date) if day is not None}
        selected_dates = [day for day in available_dates if day in key_dates]
        selected_dates.extend(
            day for day in available_dates
            if day not in key_dates and len(selected_dates) < budget.session_limit
        )
        selected_dates = sorted(selected_dates[:budget.session_limit])
        templates: list[tuple[date, str, str, int]] = []
        for index, session_date in enumerate(selected_dates):
            weekday = session_date.weekday()
            available = constraints.available_minutes_by_weekday.get(weekday, 0)
            session_type = (
                "quality"
                if session_date == quality_date
                else "long"
                if session_date == long_date
                else "recovery"
                if any(
                    (session_date - day).days == 1 and kind in {"quality", "long"}
                    for day, kind, _, _ in templates
                )
                else "aerobic"
            )
            sport = (
                focus
                if session_type in {"quality", "long"} or constraints.aerobic_mixing is False
                else sports[index % len(sports)]
            )
            exposure_cap = (
                observed_longest[sport]
                if sport in latest_sports
                else min(30, older_longest.get(sport, 0) or 30)
            )
            if sport in latest_sports and budget.mode in {
                "progressive_build", "return_to_baseline"
            }:
                exposure_cap += min(15, max(5, round(exposure_cap * 0.1)))
            if session_type == "recovery":
                prior_key = next((cap for day, kind, _, cap in reversed(templates)
                                  if (session_date - day).days == 1
                                  and kind in {"quality", "long"}), None)
                if prior_key is not None:
                    exposure_cap = min(exposure_cap, max(1, prior_key // 2))
            templates.append((session_date, session_type, sport, min(available, exposure_cap)))

        weights = {"long": 1.4, "quality": 1.0, "aerobic": 1.0, "recovery": 0.6}
        for index, (session_date, session_type, sport, cap) in enumerate(templates):
            if remaining <= 0:
                break
            future_capacity = sum(item[3] for item in templates[index + 1:])
            future_weight = sum(weights[item[1]] for item in templates[index:])
            fair_share = ceil(remaining * weights[session_type] / future_weight)
            duration = min(cap, max(fair_share, remaining - future_capacity))
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
            change_reason=(
                "provisional restart or unknown baseline; short easy sessions only"
                if sparse_baseline else "initial plan"
            ),
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
