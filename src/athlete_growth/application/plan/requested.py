"""Validate and preserve a user-specified seven-day multisport schedule."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

from athlete_growth.domain.training import PlannedSession, TrainingConstraints


def requested_sessions_for_week(
    raw_sessions: tuple[dict[str, object], ...],
    *,
    start_date: date,
    constraints: TrainingConstraints,
) -> tuple[PlannedSession, ...]:
    if not raw_sessions:
        raise ValueError("requested_sessions must contain at least one session")
    result: list[PlannedSession] = []
    day_minutes: dict[date, int] = {}
    day_orders: dict[date, set[int]] = {}
    bricks: dict[str, list[PlannedSession]] = {}
    for index, raw in enumerate(raw_sessions):
        if not isinstance(raw, dict):
            raise ValueError("each requested session must be an object")
        try:
            day = date.fromisoformat(str(raw["session_date"]))
        except (KeyError, ValueError) as exc:
            raise ValueError("session_date must be an ISO date") from exc
        if not start_date <= day < start_date + timedelta(days=7):
            raise ValueError("requested session must be within the seven-day plan")
        sport = raw.get("sport")
        if sport not in constraints.allowed_sports:
            raise ValueError("requested sport must be among allowed sports")
        duration = raw.get("duration_min")
        if type(duration) is not int or duration <= 0:
            raise ValueError("requested duration_min must be a positive integer")
        distance = raw.get("distance_m")
        if distance is not None and (type(distance) is not int or distance <= 0):
            raise ValueError("distance_m must be a positive integer in meters")
        order = raw.get("session_order", max(day_orders.get(day, set()), default=0) + 1)
        if type(order) is not int or order < 1 or order in day_orders.setdefault(day, set()):
            raise ValueError("session_order must be a distinct positive number for the day")
        day_orders[day].add(order)
        if day.weekday() in constraints.unavailable_weekdays:
            raise ValueError("requested session falls on an unavailable day")
        day_minutes[day] = day_minutes.get(day, 0) + duration
        if day_minutes[day] > constraints.available_minutes_by_weekday.get(day.weekday(), 0):
            raise ValueError("requested sessions exceed available minutes for the day")
        session_type = raw.get("session_type", "aerobic")
        if session_type not in {"aerobic", "recovery", "quality", "long", "brick"}:
            raise ValueError("invalid requested session_type")
        intensity = raw.get("intensity", "easy")
        if intensity not in {"easy", "quality"}:
            raise ValueError("requested intensity must be easy or quality")
        brick_id = raw.get("brick_id")
        if brick_id is not None and (not isinstance(brick_id, str) or not brick_id.strip()):
            raise ValueError("brick_id must be a nonempty string")
        if (brick_id is None) != (session_type != "brick"):
            raise ValueError("brick sessions require brick_id and session_type=brick")
        objective = raw.get("objective", "User-specified training session.")
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError("objective must be nonempty text")
        session = PlannedSession(
            session_id="session-" + hashlib.sha256(
                f"{start_date}:{day}:{order}:{sport}:{index}".encode()
            ).hexdigest()[:20],
            session_date=day,
            sport=str(sport),
            duration_min=duration,
            intensity=str(intensity),
            objective=objective.strip(),
            expected_load=float(duration),
            session_type=str(session_type),
            distance_m=distance,
            session_order=order,
            brick_id=brick_id,
        )
        result.append(session)
        if brick_id is not None:
            bricks.setdefault(brick_id, []).append(session)
    if sum(day_minutes.values()) > constraints.max_weekly_minutes:
        raise ValueError("requested sessions exceed max_weekly_minutes")
    for brick_id, legs in bricks.items():
        legs = sorted(legs, key=lambda item: item.session_order)
        if (len(legs) != 2 or legs[0].session_date != legs[1].session_date
            or (legs[0].sport, legs[1].sport) != ("cycling", "running")
            or legs[1].session_order != legs[0].session_order + 1):
            raise ValueError(
                f"brick {brick_id} must be consecutive cycling then running on one day"
            )
    return tuple(sorted(result, key=lambda item: (item.session_date, item.session_order)))
