"""Descriptive overnight HRV context without a readiness verdict."""

from __future__ import annotations

from datetime import date, timedelta
from statistics import mean
from zoneinfo import ZoneInfo

from athlete_growth.domain.records import CanonicalRecord, JsonValue


def overnight_hrv_context(
    records: list[CanonicalRecord], *, on: date, timezone: ZoneInfo
) -> dict[str, JsonValue]:
    """Compare like-for-like nights; never turn HRV alone into training clearance."""
    by_source: dict[str, dict[date, tuple[float, CanonicalRecord]]] = {}
    untyped_count = 0
    for record in records:
        if record.kind != "hrv" or record.quality_flags:
            continue
        value = record.data.get("last_night_avg_ms")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            if isinstance(record.data.get("hrv"), (int, float)):
                untyped_count += 1
            continue
        if not 0 < value <= 500:
            continue
        date_value = record.data.get("calendar_date")
        try:
            night = date.fromisoformat(date_value) if isinstance(date_value, str) else (
                record.occurred_at.astimezone(timezone).date()
            )
        except ValueError:
            continue
        if not on - timedelta(days=27) <= night <= on:
            continue
        source = record.evidence[0].source if record.evidence else "unknown"
        daily = by_source.setdefault(source, {})
        existing = daily.get(night)
        if existing is None or record.created_at > existing[1].created_at:
            daily[night] = (float(value), record)

    # Never mix watch vendors or measurement protocols in one comparison.
    source = max(
        by_source,
        key=lambda name: (
            max(by_source[name]),
            len(by_source[name]),
            name,
        ),
        default=None,
    )
    selected = by_source[source] if source is not None else {}
    latest_day = max(selected, default=None)
    recent_start = on - timedelta(days=6)
    recent = [value for day, (value, _) in selected.items() if recent_start <= day <= on]
    prior = [value for day, (value, _) in selected.items() if day < recent_start]
    recent_mean = round(mean(recent), 2) if len(recent) >= 3 else None
    prior_mean = round(mean(prior), 2) if len(prior) >= 7 else None
    difference = (
        round((recent_mean / prior_mean - 1) * 100, 1)
        if recent_mean is not None and prior_mean is not None and prior_mean > 0
        else None
    )
    evidence_ids: list[JsonValue] = list(
        dict.fromkeys(
            item.raw_hash or record.record_id
            for _, record in selected.values()
            for item in record.evidence
        )
    )
    if not evidence_ids:
        evidence_ids = [record.record_id for _, record in selected.values()]
    status = (
        "missing" if latest_day is None else
        "trend_available" if difference is not None else
        "insufficient_trend_history"
    )
    return {
        "status": status,
        "metric": "overnight_hrv_ms",
        "source": source,
        "latest_night_date": latest_day.isoformat() if latest_day else None,
        "latest_night_ms": round(selected[latest_day][0], 2) if latest_day else None,
        "is_current_night": latest_day == on if latest_day else False,
        "recent_7d_mean_ms": recent_mean,
        "recent_7d_nights": len(recent),
        "prior_21d_mean_ms": prior_mean,
        "prior_21d_nights": len(prior),
        "recent_vs_prior_pct": difference,
        "legacy_untyped_record_count": untyped_count,
        "evidence_ids": evidence_ids,
        "interpretation": (
            "Numerical overnight HRV context only; a single night or mean difference "
            "does not establish today's readiness. Review sleep, symptoms, resting "
            "heart rate, training load and the athlete's check-in before changing training."
        ),
        "limitations": (
            "The prior 21-day mean is a product reference, not Garmin's personal "
            "baseline or a statistically significant change. Older untyped HRV "
            "records and vendor weekly averages are excluded."
        ),
    }
