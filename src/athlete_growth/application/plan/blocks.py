"""Deterministic sport-focus rotation for a reviewable multiweek forecast."""

from __future__ import annotations

from datetime import date, timedelta

from athlete_growth.domain.training import TrainingConstraints


def choose_key_windows(
    start_date: date,
    constraints: TrainingConstraints,
    *,
    quality_weekday: int | None,
    long_weekday: int | None,
) -> tuple[int | None, int | None, dict[str, str]]:
    """Suggest windows from availability when the user has not selected them."""
    available = [
        (
            offset,
            start_date + timedelta(days=offset),
            constraints.available_minutes_by_weekday.get(
                (start_date + timedelta(days=offset)).weekday(), 0
            ),
        )
        for offset in range(7)
    ]
    available = [
        entry
        for entry in available
        if entry[1].weekday() not in constraints.unavailable_weekdays and entry[2] > 0
    ]
    sources: dict[str, str] = {}
    if long_weekday is None and available:
        chosen = max(available, key=lambda item: (item[2], item[0]))
        long_weekday = chosen[1].weekday()
        sources["long"] = "suggested_from_availability"
    elif long_weekday is not None:
        sources["long"] = "explicit_or_confirmed_preference"
    long_offset = next(
        (offset for offset, day, _ in available if day.weekday() == long_weekday), None
    )
    if quality_weekday is None and long_offset is not None:
        candidates = [item for item in available if abs(item[0] - long_offset) > 1]
        if candidates:
            chosen = max(candidates, key=lambda item: (item[2], -item[0]))
            quality_weekday = chosen[1].weekday()
            sources["quality"] = "suggested_from_availability"
    elif quality_weekday is not None:
        sources["quality"] = "explicit_or_confirmed_preference"
    return quality_weekday, long_weekday, sources


def build_block_cycle(
    *,
    start_date: date,
    weeks: int,
    allowed_sports: tuple[str, ...],
    focus_sequence: tuple[str, ...],
    preferred_focus: str | None,
    quality_weekday: int | None,
    long_weekday: int | None,
    key_window_sources: dict[str, str],
    source: str,
    base_constraints: dict[str, object],
) -> dict[str, object]:
    if not 2 <= weeks <= 12:
        raise ValueError("block weeks must be between 2 and 12")
    if not allowed_sports:
        raise ValueError("at least one block sport is required")
    if focus_sequence:
        if len(focus_sequence) > 12 or any(sport not in allowed_sports for sport in focus_sequence):
            raise ValueError("block focus sequence must use selected sports")
        sequence = focus_sequence
    else:
        sequence = allowed_sports
        if preferred_focus in sequence:
            index = sequence.index(preferred_focus)
            sequence = sequence[index:] + sequence[:index]
    forecast = []
    for index in range(weeks):
        beginning = start_date + timedelta(days=index * 7)
        forecast.append(
            {
                "week_index": index,
                "start_date": beginning.isoformat(),
                "end_date": (beginning + timedelta(days=6)).isoformat(),
                "focus_sport": sequence[index % len(sequence)],
                "status": "current_week_draft" if index == 0 else "forecast_only",
            }
        )
    return {
        "anchor_start_date": start_date.isoformat(),
        "weeks": weeks,
        "focus_sequence": list(sequence),
        "focus_source": source,
        "preferred_quality_weekday": quality_weekday,
        "preferred_long_weekday": long_weekday,
        "key_window_sources": key_window_sources,
        "base_constraints": base_constraints,
        "forecast": forecast,
        "future_weeks_require_fresh_preview": True,
    }


def focus_for_date(cycle: dict[str, object], start_date: date) -> str:
    anchor = date.fromisoformat(str(cycle["anchor_start_date"]))
    sequence = cycle["focus_sequence"]
    if not isinstance(sequence, list) or not sequence:
        raise ValueError("invalid block cycle")
    offset_days = (start_date - anchor).days
    if offset_days % 7:
        raise ValueError("block update must start on a forecast week boundary")
    index = offset_days // 7
    if index < 0 or index >= int(str(cycle["weeks"])):
        raise ValueError("block cycle ended; preview new blocks")
    return str(sequence[index % len(sequence)])


def add_progression_envelope(
    cycle: dict[str, object],
    *,
    observed_first_week_minutes: int,
    goal_type: str,
    goal_deadline: date | None,
) -> dict[str, object]:
    """Forecast bounded weekly ceilings, never auto-approve future sessions."""
    if observed_first_week_minutes <= 0:
        raise ValueError("first week must contain observed-duration sessions")
    forecast = cycle.get("forecast")
    if not isinstance(forecast, list):
        raise ValueError("invalid block forecast")
    base = cycle.get("base_constraints")
    if not isinstance(base, dict):
        raise ValueError("invalid block constraints")
    maximum = int(str(base["max_weekly_minutes"]))
    current = observed_first_week_minutes
    enriched: list[dict[str, object]] = []
    for index, raw in enumerate(forecast):
        if not isinstance(raw, dict):
            raise ValueError("invalid block week")
        start = date.fromisoformat(str(raw["start_date"]))
        end = date.fromisoformat(str(raw["end_date"]))
        event_week = (
            goal_type == "race"
            and goal_deadline is not None
            and start <= goal_deadline <= end
        )
        taper_week = (
            goal_type == "race"
            and goal_deadline is not None
            and end < goal_deadline <= end + timedelta(days=7)
        )
        if index == 0:
            phase = "current_week"
            ceiling = current
        elif event_week:
            phase = "event_week_review"
            ceiling = min(maximum, max(1, round(current * 0.6)))
        elif taper_week:
            phase = "taper_forecast"
            ceiling = min(maximum, max(1, round(current * 0.8)))
        elif index % 4 == 3:
            phase = "recovery_forecast"
            ceiling = min(maximum, max(1, round(current * 0.8)))
        else:
            phase = "build_forecast"
            current = min(maximum, max(current, round(current * 1.05)))
            ceiling = current
        enriched.append({
            **raw,
            "phase": phase,
            "weekly_ceiling_min": ceiling,
            "ceiling_basis": "first-week observed-duration preview; conservative heuristic",
            "requires_fresh_evidence_and_confirmation": index > 0,
        })
    return {
        **cycle,
        "forecast": enriched,
        "goal_deadline": goal_deadline.isoformat() if goal_deadline else None,
        "progression_interpretation": (
            "Forecast ceilings are planning guardrails, not prescribed load or injury clearance. "
            "Future sessions require fresh activity, subjective state and a new confirmed preview."
        ),
    }
