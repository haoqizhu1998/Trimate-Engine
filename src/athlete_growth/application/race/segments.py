"""Arithmetic preview of user-supplied endurance race segment targets."""

from __future__ import annotations

import math
from datetime import date

_SPORTS = {"running", "cycling", "swimming", "triathlon"}


def _positive(value: object, maximum: float, message: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(message)
    numeric = float(value)
    if not math.isfinite(numeric) or not 0 < numeric <= maximum:
        raise ValueError(message)
    return numeric


def preview_race_segments(
    *, event_name: str, event_date: date, sport: str, segments: list[dict[str, object]]
) -> dict[str, object]:
    if not event_name.strip() or len(event_name) > 200:
        raise ValueError("event name is invalid")
    if sport not in _SPORTS or not 1 <= len(segments) <= 5:
        raise ValueError("race segments are invalid")
    expected = (
        ["swimming", "transition", "cycling", "transition", "running"]
        if sport == "triathlon"
        else [sport]
    )
    if [segment.get("sport") for segment in segments] != expected:
        raise ValueError("race segments do not match event sport")
    result = []
    total = 0.0
    for index, segment in enumerate(segments, start=1):
        segment_sport = str(segment["sport"])
        duration = _positive(
            segment.get("target_duration_min"), 10080, "race segment target is invalid"
        )
        distance = segment.get("distance_km")
        if segment_sport == "transition":
            if distance is not None:
                raise ValueError("transition must not have distance")
            metrics: dict[str, float] = {}
        else:
            distance = _positive(distance, 10000, "race segment distance is invalid")
            metrics = {
                "running": {"pace_min_per_km": round(duration / distance, 3)},
                "cycling": {"speed_km_per_hour": round(distance / (duration / 60), 3)},
                "swimming": {"pace_min_per_100m": round(duration / (distance * 10), 3)},
            }[segment_sport]
        total += duration
        result.append(
            {
                "index": index,
                "sport": segment_sport,
                "distance_km": distance,
                "target_duration_min": duration,
                "derived_target": metrics,
            }
        )
    return {
        "event_name": event_name.strip(),
        "event_date": event_date.isoformat(),
        "sport": sport,
        "segments": result,
        "total_target_min": round(total, 2),
        "target_source": "user supplied; unverified",
        "data_gaps": [
            "Targets are arithmetic only; current ability, course, weather and fueling "
            "are unverified",
            "Test race-specific pacing and transitions in training before relying on this preview",
        ],
        "saved": False,
    }
