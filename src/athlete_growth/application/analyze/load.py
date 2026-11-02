"""Comparable training-load windows for Trimate."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from athlete_growth.domain.records import CanonicalRecord


@dataclass(frozen=True, slots=True)
class LoadWindow:
    method: str | None
    acute: float | None
    chronic_weekly: float | None
    ratio: float | None
    comparable_count: int
    total_count: int
    gaps: tuple[str, ...]


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _sample(record: CanonicalRecord) -> tuple[str, float] | None:
    load = _number(record.data.get("training_load"))
    if load is not None:
        source = record.evidence[0].source if record.evidence else "unattributed"
        return (f"{source}:training_load", load)
    duration = _number(record.data.get("duration_min"))
    rpe = _number(record.data.get("rpe"))
    if duration is not None and duration > 0 and rpe is not None and rpe <= 10:
        return ("session_rpe", duration * rpe)
    return None


def comparable_load_window(
    activities: list[CanonicalRecord], *, as_of: datetime, acute_start: datetime
) -> LoadWindow:
    """Select the latest score family and require prior-week coverage for a ratio.

    A ratio is descriptive context only. It is never an injury-risk score.
    """

    samples = [
        (record, sample[0], sample[1])
        for record in activities
        if record.occurred_at <= as_of and (sample := _sample(record)) is not None
    ]
    if not samples:
        return LoadWindow(
            None, None, None, None, 0, len(activities),
            ("comparable training-load scores unavailable",),
        )
    method = max(samples, key=lambda item: item[0].occurred_at)[1]
    comparable = [item for item in samples if item[1] == method]
    acute = sum(value for record, _, value in comparable if record.occurred_at >= acute_start)
    prior_weeks = {
        index
        for record, _, _ in comparable
        for index in range(1, 4)
        if as_of - timedelta(days=7 * (index + 1)) < record.occurred_at
        <= as_of - timedelta(days=7 * index)
    }
    complete_method_coverage = len(comparable) == len(activities)
    baseline_ready = (
        len(prior_weeks) == 3
        and complete_method_coverage
        and not method.startswith("unattributed:")
    )
    chronic = (
        sum(value for record, _, value in comparable if record.occurred_at < acute_start) / 3
        if baseline_ready else None
    )
    ratio = acute / chronic if chronic is not None and chronic > 0 else None
    gaps = []
    if not complete_method_coverage:
        gaps.append("activity load scores use different or missing methods")
    if not baseline_ready:
        gaps.append("three prior weeks of attributed, same-method load are required for a ratio")
    if method.startswith("unattributed:"):
        gaps.append("device-load source is unattributed")
    return LoadWindow(
        method,
        round(acute, 2),
        round(chronic, 2) if chronic is not None else None,
        round(ratio, 3) if ratio is not None else None,
        len(comparable),
        len(activities),
        tuple(gaps),
    )
