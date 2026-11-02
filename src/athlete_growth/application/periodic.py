"""Evidence-bounded calendar summaries for TriMate's periodic summary Skill."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from athlete_growth.ports.repositories import CanonicalRecordRepository

SPORT_LABELS = {
    "running": "跑步", "cycling": "骑行", "swimming": "游泳", "triathlon": "铁三",
}
SPORT_ICONS = {
    "running": "run", "cycling": "bike", "swimming": "swim", "triathlon": "tri",
}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _sport(value: object) -> str:
    if not isinstance(value, str):
        return "other"
    value = value.lower().replace(" ", "_")
    if "run" in value:
        return "running"
    if "cycl" in value or "bik" in value:
        return "cycling"
    if "swim" in value:
        return "swimming"
    if "triathlon" in value or "multisport" in value:
        return "triathlon"
    return "other"


def period_bounds(cycle_type: str, anchor: date, today: date) -> tuple[date, date, bool]:
    if anchor > today:
        raise ValueError("period anchor is in the future")
    if cycle_type == "week":
        start = anchor - timedelta(days=anchor.weekday())
        planned_end = start + timedelta(days=6)
    elif cycle_type == "4weeks":
        planned_end = anchor - timedelta(days=(anchor.weekday() + 1) % 7)
        start = planned_end - timedelta(days=27)
    elif cycle_type == "month":
        start = anchor.replace(day=1)
        next_month = (start.replace(year=start.year + 1, month=1)
                      if start.month == 12 else start.replace(month=start.month + 1))
        planned_end = next_month - timedelta(days=1)
    elif cycle_type == "quarter":
        month = ((anchor.month - 1) // 3) * 3 + 1
        start = anchor.replace(month=month, day=1)
        next_quarter = (start.replace(year=start.year + 1, month=1)
                        if month == 10 else start.replace(month=month + 3))
        planned_end = next_quarter - timedelta(days=1)
    elif cycle_type == "year":
        start = anchor.replace(month=1, day=1)
        planned_end = start.replace(year=start.year + 1) - timedelta(days=1)
    else:
        raise ValueError("unsupported summary cycle")
    end = min(planned_end, today)
    return start, end, end < planned_end


class PeriodicSummaryService:
    def __init__(
        self, athlete_id: str, records: CanonicalRecordRepository,
        *, timezone_name: str = "UTC", now: datetime | None = None,
    ) -> None:
        self.athlete_id = athlete_id
        self.records = records
        self.timezone = ZoneInfo(timezone_name)
        self.now = now or datetime.now(UTC)

    def summary(self, cycle_type: str, anchor: date | None = None) -> dict[str, object]:
        today = self.now.astimezone(self.timezone).date()
        start, end, partial = period_bounds(cycle_type, anchor or today, today)
        selected = [
            record for record in self.records.list_current(self.athlete_id)
            if start <= record.occurred_at.astimezone(self.timezone).date() <= end
            and record.occurred_at <= self.now
        ]
        activities = [record for record in selected if record.kind == "activity"]
        by_sport: dict[str, dict[str, float | int]] = defaultdict(
            lambda: {"sessions": 0, "duration_min": 0.0, "distance_m": 0.0}
        )
        by_day: dict[date, list[dict[str, object]]] = defaultdict(list)
        for record in activities:
            sport = _sport(record.data.get("sport"))
            duration = _number(record.data.get("duration_min")) or 0.0
            distance = _number(record.data.get("distance_m")) or 0.0
            row = by_sport[sport]
            row["sessions"] += 1
            row["duration_min"] += duration
            row["distance_m"] += distance
            day = record.occurred_at.astimezone(self.timezone).date()
            by_day[day].append({
                "sport": sport, "duration_min": round(duration, 2),
                "distance_m": round(distance, 2),
            })
        body_rows = [
            (record.occurred_at, value)
            for record in selected if record.kind == "body-weight"
            if (value := _number(record.data.get("weight_kg"))) is not None
        ]
        body_rows.sort()
        body_data: dict[str, float] = {}
        if len(body_rows) >= 2:
            body_data = {
                "weight_start": round(body_rows[0][1], 2),
                "weight_end": round(body_rows[-1][1], 2),
                "weight_change": round(body_rows[-1][1] - body_rows[0][1], 2),
            }
        rows = [
            {"sport": sport, **{
                key: round(value, 2) if isinstance(value, float) else value
                for key, value in metrics.items()
            }}
            for sport, metrics in sorted(by_sport.items())
        ]
        duration_total = sum(float(row["duration_min"]) for row in rows)
        daily = [
            {"date": day.isoformat(), "activities": by_day[day]}
            for day in sorted(by_day)
        ]
        return {
            "cycle_type": cycle_type,
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "partial": partial,
            "training_data": {
                "total_sessions": len(activities),
                "total_duration_minutes": round(duration_total, 2),
                "avg_duration_minutes": round(duration_total / len(activities), 2)
                if activities else None,
                "discipline_breakdown": rows,
            },
            "daily_details": daily,
            "body_data": body_data,
            "data_gaps": (
                (["no activities in period"] if not activities else [])
                + ["plan completion not calculated without session mapping"]
            ),
            "interpretation": "Only observed records are summarized; missing fields are omitted.",
        }


def poster_payload(summary: dict[str, object]) -> dict[str, object]:
    training = summary["training_data"]
    assert isinstance(training, dict)
    breakdown = training["discipline_breakdown"]
    assert isinstance(breakdown, list)
    cycle = str(summary["cycle_type"])
    title = {"week": "MY WEEK", "4weeks": "MY BLOCK", "month": "MY MONTH",
             "quarter": "MY QUARTER", "year": "MY YEAR"}[cycle]
    totals = [
        {
            "label": SPORT_LABELS.get(str(row["sport"]), "其他"),
            "value": f"{row['sessions']}次",
            "subvalue": f"{float(row['distance_m']) / 1000:.1f} km",
            "icon": SPORT_ICONS.get(str(row["sport"]), "run"),
        }
        for row in breakdown if isinstance(row, dict)
    ]
    daily = summary["daily_details"]
    assert isinstance(daily, list)
    by_date = {
        str(row["date"]): row["activities"]
        for row in daily if isinstance(row, dict)
    }
    end_date = date.fromisoformat(str(summary["end_date"]))
    start_date = date.fromisoformat(str(summary["start_date"]))
    first_timeline_day = max(start_date, end_date - timedelta(days=6))
    timeline = [
        {
            "day": day.strftime("%a"),
            "date": day.strftime("%m/%d"),
            "title": "训练日" if by_date.get(day.isoformat()) else "未记录训练",
            "details": [
                f"{SPORT_LABELS.get(str(item['sport']), '其他')} "
                f"{item['duration_min']} 分钟"
                for item in by_date.get(day.isoformat(), []) if isinstance(item, dict)
            ],
            "tone": "normal", "icon": "run",
        }
        for day in (
            first_timeline_day + timedelta(days=offset)
            for offset in range((end_date - first_timeline_day).days + 1)
        )
    ]
    return {
        "title": title, "period": cycle.upper(), "main_title": "训练总结",
        "date_range": f"{summary['start_date']} — {summary['end_date']}",
        "headline": "Train Hard, Recover Smart.",
        "weekly_totals": totals,
        "key_metrics": [
            {"label": "训练次数", "value": str(training["total_sessions"]), "icon": "bars"},
            {"label": "总时长", "value": f"{float(training['total_duration_minutes']) / 60:.1f} h",
             "icon": "clock"},
        ],
        "insights": "仅展示已记录的训练数据。",
        "recommendations": [],
        "timeline": timeline,
    }
