"""Conservative edits to one upcoming session in an approved seven-day plan."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from athlete_growth.domain.training import PlannedSession, TrainingConstraints

ADJUSTMENT_ACTIONS = frozenset({"skip", "shorten", "substitute", "move"})


def adjust_upcoming_session(
    sessions: tuple[PlannedSession, ...],
    *,
    session_id: str,
    action: str,
    today: date,
    constraints: TrainingConstraints,
    week_start: date,
    new_date: date | None = None,
    new_sport: str | None = None,
    new_duration_min: int | None = None,
    observed_longest: dict[str, int] | None = None,
) -> tuple[tuple[PlannedSession, ...], dict[str, object]]:
    if action not in ADJUSTMENT_ACTIONS:
        raise ValueError("plan adjustment action is invalid")
    current = next((item for item in sessions if item.session_id == session_id), None)
    if current is None:
        raise ValueError("planned session not found")
    if current.session_date < today:
        raise ValueError("past sessions cannot be edited; record the actual workout")
    first_day = week_start
    last_day = first_day + timedelta(days=6)
    changed: PlannedSession | None
    if action == "skip":
        if new_date is not None or new_sport is not None or new_duration_min is not None:
            raise ValueError("skip does not accept replacement fields")
        changed = None
    elif action == "shorten":
        if new_date is not None or new_sport is not None:
            raise ValueError("shorten only accepts a smaller duration")
        if type(new_duration_min) is not int or not 0 < new_duration_min < current.duration_min:
            raise ValueError("shorten only accepts a smaller duration")
        key_downgrade = current.session_type in {"quality", "long"} and (
            new_duration_min < current.duration_min // 2 or new_duration_min <= 20
        )
        changed = replace(
            current,
            duration_min=new_duration_min,
            expected_load=float(new_duration_min),
            intensity="easy" if key_downgrade else current.intensity,
            session_type="aerobic" if key_downgrade else current.session_type,
            objective="Shortened by user request; keep effort comfortable."
            if key_downgrade
            else current.objective,
        )
    elif action == "substitute":
        if new_date is not None or new_sport not in constraints.allowed_sports:
            raise ValueError("substitute sport must be in the plan")
        if new_sport == current.sport:
            raise ValueError("substitute sport must differ from the planned sport")
        cap = (observed_longest or {}).get(new_sport, 0) or 45
        duration = current.duration_min if new_duration_min is None else new_duration_min
        if type(duration) is not int or not 0 < duration <= min(current.duration_min, cap):
            raise ValueError("substitute duration exceeds the conservative cap")
        changed = replace(
            current,
            sport=new_sport,
            duration_min=duration,
            expected_load=float(duration),
            intensity="easy",
            session_type="aerobic",
            objective="User-requested easy sport substitution; no makeup key session.",
        )
    else:
        if new_sport is not None or new_duration_min is not None or new_date is None:
            raise ValueError("move requires only a new date")
        if not today <= new_date <= last_day or new_date < first_day:
            raise ValueError("moved session must stay in the current plan week")
        if new_date == current.session_date:
            raise ValueError("moved session needs a different date")
        if any(item.session_date == new_date for item in sessions if item.session_id != session_id):
            raise ValueError("moved session collides with another planned session")
        weekday = new_date.weekday()
        if weekday in constraints.unavailable_weekdays or (
            constraints.available_minutes_by_weekday.get(weekday, 0) < current.duration_min
        ):
            raise ValueError("moved session exceeds available time")
        if current.session_type in {"quality", "long"} and any(
            item.session_type in {"quality", "long"}
            and abs((item.session_date - new_date).days) <= 1
            for item in sessions
            if item.session_id != session_id
        ):
            raise ValueError("key sessions need a day between them")
        changed = replace(current, session_date=new_date)
    result = tuple(
        sorted(
            (
                changed if item.session_id == session_id else item
                for item in sessions
                if item.session_id != session_id or changed is not None
            ),
            key=lambda item: (item.session_date, item.session_id),
        )
    )
    details = {
        "action": action,
        "session_id": session_id,
        "before": _session_view(current),
        "after": _session_view(changed) if changed is not None else None,
        "weekly_minutes_change": (changed.duration_min if changed else 0) - current.duration_min,
        "makeup_session_added": False,
    }
    return result, details


def _session_view(session: PlannedSession) -> dict[str, object]:
    return {
        "date": session.session_date.isoformat(),
        "sport": session.sport,
        "duration_min": session.duration_min,
        "intensity": session.intensity,
        "session_type": session.session_type,
    }
