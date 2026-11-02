"""Host-neutral, bounded product capabilities for conversational adapters.

No function accepts a vault path from a model, returns credentials or raw evidence,
or silently writes a generated goal, plan, race strategy, or nutrition preview.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from athlete_growth.adapters.storage import ProfileRegistry
from athlete_growth.application.check_in import CheckInService
from athlete_growth.application.data_quality import inspect_records
from athlete_growth.application.learn import PersonalResponseLearner
from athlete_growth.application.nutrition import NutritionPlanner
from athlete_growth.application.nutrition.logistics import (
    preview_fueling_logistics as fueling_logistics_preview,
)
from athlete_growth.application.performance import EndurancePerformanceService
from athlete_growth.application.race import RaceStrategyService
from athlete_growth.application.race.segments import preview_race_segments as race_segment_preview
from athlete_growth.application.recovery import ReturnToTrainingService
from athlete_growth.application.report import ReportService
from athlete_growth.domain.goals import Goal, GoalType, detect_conflicts
from athlete_growth.domain.nutrition import FuelingContext, NutritionProfile
from athlete_growth.domain.race import RaceEvent
from athlete_growth.domain.recovery import InjuryStatus
from athlete_growth.interfaces.host.tools import HostRuntime, automation_status

_ACTIVITY_FIELDS = (
    "sport",
    "duration_min",
    "distance_m",
    "avg_hr",
    "max_hr",
    "calories",
    "training_load",
)
_RECORD_LIMIT = 50


def _json_safe(value: dict[str, object]) -> dict[str, object]:
    def convert(item: object) -> str:
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        if isinstance(item, Enum):
            return str(item.value)
        raise TypeError(f"unsupported host value: {type(item).__name__}")

    return cast(dict[str, object], json.loads(json.dumps(value, default=convert)))


def onboarding_status(data_home: str | Path) -> dict[str, object]:
    home = Path(data_home).expanduser().resolve()
    profile_id = ProfileRegistry(home / "config.toml").default_profile_id()
    if profile_id is None:
        return {
            "profile_ready": False,
            "sync_configured": False,
            "first_sync_completed": False,
            "next_step": "Initialize a private athlete profile through the local onboarding flow.",
        }
    status = automation_status(home)
    policies = status["policies"]
    assert isinstance(policies, list)
    first_sync = any(
        isinstance(item, dict)
        and isinstance(item.get("last_run"), dict)
        and item["last_run"].get("status") == "completed"
        for item in policies
    )
    return {
        "profile_ready": True,
        "sync_configured": bool(policies),
        "first_sync_completed": first_sync,
        "next_step": (
            "You can ask for a daily or weekly report, or describe a training goal."
            if first_sync
            else "Complete Garmin China login and first sync locally, or import a supported file."
        ),
    }


def profile_summary(data_home: str | Path) -> dict[str, object]:
    runtime = HostRuntime.load(data_home)
    profile = runtime.profiles.get_by_id(runtime.athlete_id)
    if profile is None:
        raise RuntimeError("default profile record is missing")
    return {
        "display_name": profile.display_name,
        "timezone": profile.timezone_name,
        "units": profile.unit_system,
        "created_at": profile.created_at.isoformat(),
    }


def daily_report(data_home: str | Path, on: date | None = None) -> dict[str, object]:
    runtime = HostRuntime.load(data_home)
    profile = runtime.profiles.get_by_id(runtime.athlete_id)
    if profile is None:
        raise RuntimeError("default profile record is missing")
    return _json_safe(
        asdict(
            ReportService(
                runtime.athlete_id, runtime.records, timezone_name=profile.timezone_name
            ).daily(on or datetime.now(UTC).astimezone(ZoneInfo(profile.timezone_name)).date())
        )
    )


def weekly_report(data_home: str | Path, end_on: date | None = None) -> dict[str, object]:
    runtime = HostRuntime.load(data_home)
    profile = runtime.profiles.get_by_id(runtime.athlete_id)
    if profile is None:
        raise RuntimeError("default profile record is missing")
    return _json_safe(
        asdict(
            ReportService(
                runtime.athlete_id, runtime.records, timezone_name=profile.timezone_name
            ).weekly(end_on or datetime.now(UTC).astimezone(ZoneInfo(profile.timezone_name)).date())
        )
    )


def goals(data_home: str | Path) -> dict[str, object]:
    runtime = HostRuntime.load(data_home)
    selected: list[Goal] = []
    for record in runtime.records.list_current(runtime.athlete_id, kind="goal"):
        data = record.data
        deadline = data.get("deadline")
        selected.append(
            Goal(
                goal_id=str(data["goal_id"]),
                athlete_id=runtime.athlete_id,
                goal_type=cast(GoalType, str(data["goal_type"])),
                title=str(data["title"]),
                priority=int(str(data["priority"])),
                deadline=date.fromisoformat(str(deadline)) if deadline else None,
                target_metric=str(data["target_metric"]) if data.get("target_metric") else None,
                target_value=float(str(data["target_value"]))
                if data.get("target_value") is not None
                else None,
                status=str(data.get("status", "active")),
            )
        )
    return _json_safe(
        {
            "goals": [asdict(item) for item in selected],
            "conflicts": [asdict(item) for item in detect_conflicts(selected)],
        }
    )


def activities(data_home: str | Path, *, limit: int = 10) -> dict[str, object]:
    if not 1 <= limit <= _RECORD_LIMIT:
        raise ValueError("limit must be between 1 and 50")
    runtime = HostRuntime.load(data_home)
    records = runtime.records.list_current(runtime.athlete_id, kind="activity")
    recent = sorted(records, key=lambda record: record.occurred_at, reverse=True)[:limit]
    return {
        "activities": [
            {
                "record_id": item.record_id,
                "occurred_at": item.occurred_at.isoformat(),
                "metrics": {key: item.data[key] for key in _ACTIVITY_FIELDS if key in item.data},
                "quality_flags": list(item.quality_flags),
                "source_count": len(item.evidence),
            }
            for item in recent
        ],
        "total_count": len(records),
    }


def _performance(data_home: str | Path) -> EndurancePerformanceService:
    runtime = HostRuntime.load(data_home)
    profile = runtime.profiles.get_by_id(runtime.athlete_id)
    if profile is None:
        raise RuntimeError("default profile record is missing")
    return EndurancePerformanceService(
        runtime.athlete_id,
        runtime.records,
        timezone_name=profile.timezone_name,
    )


def activity_detail(data_home: str | Path, record_id: str) -> dict[str, object]:
    return _performance(data_home).get_activity_detail(record_id)


def session_analysis(data_home: str | Path, record_id: str) -> dict[str, object]:
    return _performance(data_home).analyze_session(record_id)


def sport_baseline(
    data_home: str | Path, sport: str, *, days: int = 90
) -> dict[str, object]:
    return _performance(data_home).get_sport_baseline(sport, days=days)


def training_load_context(
    data_home: str | Path, *, sport: str | None = None, end_date: date | None = None
) -> dict[str, object]:
    return _performance(data_home).get_training_load_context(sport=sport, end_date=end_date)


def _bounded_check_in_responses(data: Mapping[str, object]) -> dict[str, int | float | bool]:
    responses: dict[str, int | float | bool] = {}
    for key in ("fatigue", "soreness", "pain", "mood", "motivation", "rpe"):
        value = data.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and 0 <= value <= 10
        ):
            responses[key] = value
    illness = data.get("illness")
    if isinstance(illness, bool):
        responses["illness"] = illness
    return responses


def recent_check_ins(data_home: str | Path, *, days: int = 28) -> dict[str, object]:
    if not 7 <= days <= 90:
        raise ValueError("check-in history window is invalid")
    runtime = HostRuntime.load(data_home)
    now = datetime.now(UTC)
    start = now - timedelta(days=days)
    records = sorted(
        (
            record
            for record in runtime.records.list_current(runtime.athlete_id, kind="check-in")
            if start <= record.occurred_at <= now
        ),
        key=lambda record: record.occurred_at,
        reverse=True,
    )[:30]
    return {
        "window_days": days,
        "check_ins": [
            {
                "record_id": record.record_id,
                "occurred_at": record.occurred_at.isoformat(),
                "responses": _bounded_check_in_responses(record.data),
            }
            for record in records
        ],
        "status": "observed" if records else "unknown",
        "interpretation": "missing check-ins are unknown, never evidence of good recovery",
    }


def plan_actual_comparison(
    data_home: str | Path, plan_id: str, session_id: str, activity_id: str
) -> dict[str, object]:
    return _performance(data_home).compare_plan_actual(plan_id, session_id, activity_id)


def preview_race_segments(
    *, event_name: str, event_date: date, sport: str, segments: list[dict[str, object]]
) -> dict[str, object]:
    return race_segment_preview(
        event_name=event_name, event_date=event_date, sport=sport, segments=segments
    )


def preview_fueling_logistics(
    *,
    duration_min: int,
    declared_tested_carbs_g_per_hour: float,
    carbs_per_serving_g: float,
    available_servings: int,
    opportunity_times_min: list[int],
) -> dict[str, object]:
    return fueling_logistics_preview(
        duration_min=duration_min,
        declared_tested_carbs_g_per_hour=declared_tested_carbs_g_per_hour,
        carbs_per_serving_g=carbs_per_serving_g,
        available_servings=available_servings,
        opportunity_times_min=opportunity_times_min,
    )


def data_quality(data_home: str | Path) -> dict[str, object]:
    runtime = HostRuntime.load(data_home)
    records = runtime.records.list_current(runtime.athlete_id)
    findings = inspect_records(records)
    return {
        "record_count": len(records),
        "finding_count": len(findings),
        "findings": [asdict(item) for item in findings[:_RECORD_LIMIT]],
        "truncated": len(findings) > _RECORD_LIMIT,
    }


def training_plans(data_home: str | Path, *, limit: int = 5) -> dict[str, object]:
    if not 1 <= limit <= 10:
        raise ValueError("limit must be between 1 and 10")
    runtime = HostRuntime.load(data_home)
    records = runtime.records.list_current(runtime.athlete_id, kind="training-plan")
    recent = sorted(records, key=lambda record: record.occurred_at, reverse=True)[:limit]
    return {
        "plans": [{"record_id": item.record_id, "plan": item.data} for item in recent],
        "total_count": len(records),
    }


def growth_history(data_home: str | Path, *, limit: int = 10) -> dict[str, object]:
    if not 1 <= limit <= _RECORD_LIMIT:
        raise ValueError("limit must be between 1 and 50")
    runtime = HostRuntime.load(data_home)
    kinds = ("recommendation-feedback", "growth-milestone")
    records = [
        item
        for kind in kinds
        for item in runtime.records.list_current(runtime.athlete_id, kind=kind)
    ]
    recent = sorted(records, key=lambda record: record.occurred_at, reverse=True)[:limit]
    return {
        "items": [
            {"kind": item.kind, "occurred_at": item.occurred_at.isoformat(), "data": item.data}
            for item in recent
        ],
        "total_count": len(records),
    }


def preview_check_in(
    data_home: str | Path,
    *,
    occurred_at: datetime,
    fatigue: int,
    soreness: int,
    pain: int,
    mood: int,
    motivation: int,
    illness: bool,
    rpe: int | None = None,
    work_stress: int = 0,
    travel: bool = False,
    alcohol: bool = False,
) -> dict[str, object]:
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise ValueError("check-in timestamp must include a timezone")
    if occurred_at.astimezone(UTC) > datetime.now(UTC) + timedelta(minutes=5):
        raise ValueError("check-in timestamp cannot be in the future")
    scores = {
        "fatigue": fatigue,
        "soreness": soreness,
        "pain": pain,
        "mood": mood,
        "motivation": motivation,
    }
    if rpe is not None:
        scores["rpe"] = rpe
    scores["work_stress"] = work_stress
    if any(type(value) is not int or not 0 <= value <= 10 for value in scores.values()):
        raise ValueError("check-in scores must be integers between 0 and 10")
    if any(type(value) is not bool for value in (illness, travel, alcohol)):
        raise ValueError("check-in flags must be booleans")
    payload = {
        "occurred_at": occurred_at.isoformat(),
        "fatigue": fatigue,
        "soreness": soreness,
        "pain": pain,
        "mood": mood,
        "motivation": motivation,
        "illness": illness,
        "rpe": rpe,
        "context": {"work_stress": work_stress, "travel": travel, "alcohol": alcohol},
    }
    home = Path(data_home).expanduser().resolve()
    athlete_id = ProfileRegistry(home / "config.toml").default_profile_id()
    if athlete_id is None:
        raise RuntimeError("data home is not initialized")
    fingerprint = hashlib.sha256(
        json.dumps(
            {"athlete_id": athlete_id, "check_in": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {"check_in": payload, "preview_hash": fingerprint, "requires_confirmation": True}


def confirm_check_in(
    data_home: str | Path, *, preview_hash: str, **values: object
) -> dict[str, object]:
    preview = preview_check_in(data_home, **values)  # type: ignore[arg-type]
    if preview["preview_hash"] != preview_hash:
        raise ValueError("check-in changed; preview it again")
    runtime = HostRuntime.load(data_home)
    payload = preview["check_in"]
    assert isinstance(payload, dict)
    context = payload["context"]
    assert isinstance(context, dict)
    record = CheckInService(runtime.athlete_id, runtime.records).record(
        occurred_at=datetime.fromisoformat(str(payload["occurred_at"])),
        fatigue=int(str(payload["fatigue"])),
        soreness=int(str(payload["soreness"])),
        pain=int(str(payload["pain"])),
        mood=int(str(payload["mood"])),
        motivation=int(str(payload["motivation"])),
        illness=bool(payload["illness"]),
        rpe=int(str(payload["rpe"])) if payload["rpe"] is not None else None,
        context=context,
    )
    profile = runtime.profiles.get_by_id(runtime.athlete_id)
    if profile is None:
        raise RuntimeError("default profile record is missing")
    local_day = record.occurred_at.astimezone(ZoneInfo(profile.timezone_name)).date()
    report = daily_report(data_home, local_day)
    safety = report.get("safety")
    return {
        "record_id": record.record_id,
        "confirmed": True,
        "check_in": payload,
        "updated_report": {
            "period": report.get("period"),
            "confidence": report.get("confidence"),
            "data_gaps": report.get("data_gaps"),
            "safety": safety,
        },
    }


def assess_return_to_training(
    *,
    body_area: str,
    started_on: date,
    pain: int,
    swelling: bool,
    normal_daily_function: bool,
    stable_days: int,
) -> dict[str, object]:
    if not body_area.strip() or len(body_area) > 100 or stable_days < 0:
        raise ValueError("body area and stable days are invalid")
    injury = InjuryStatus(uuid4().hex, body_area.strip(), started_on, "recovering", "user reported")
    return asdict(
        ReturnToTrainingService().assess(
            injury=injury,
            pain=pain,
            swelling=swelling,
            normal_daily_function=normal_daily_function,
            stable_days=stable_days,
        )
    )


def preview_fueling_plan(
    *,
    weight_kg: float,
    duration_min: int,
    intensity: str,
    is_race: bool,
    temperature_c: float | None = None,
    sweat_rate_ml_per_hour: int | None = None,
    preferences: tuple[str, ...] = (),
    allergies: tuple[str, ...] = (),
) -> dict[str, object]:
    if not math.isfinite(weight_kg) or not 20 <= weight_kg <= 400:
        raise ValueError("weight_kg must be between 20 and 400")
    if not 1 <= duration_min <= 1440 or intensity not in {"easy", "moderate", "hard"}:
        raise ValueError("duration or intensity is invalid")
    if temperature_c is not None and (
        not math.isfinite(temperature_c) or not -50 <= temperature_c <= 60
    ):
        raise ValueError("temperature is invalid")
    if sweat_rate_ml_per_hour is not None and not 1 <= sweat_rate_ml_per_hour <= 5000:
        raise ValueError("sweat rate is invalid")
    if len(preferences) > 20 or len(allergies) > 20:
        raise ValueError("too many dietary entries")
    profile = NutritionProfile(weight_kg, preferences, allergies, sweat_rate_ml_per_hour)
    context = FuelingContext(duration_min, intensity, is_race, temperature_c)
    return {
        "plan": asdict(NutritionPlanner().plan(profile, context)),
        "allergies_reported": list(allergies),
        "caution": "Avoid reported allergens; this plan does not verify product ingredients."
        if allergies
        else None,
        "saved": False,
    }


def preview_race_strategy(
    *,
    sport: str = "running",
    event_name: str,
    event_date: date,
    distance_km: float,
    target_finish_min: float,
    elevation_gain_m: float = 0,
    location: str = "",
    temperature_c: float | None = None,
    wind_kph: float | None = None,
    timezone_shift_hours: float = 0,
) -> dict[str, object]:
    if sport != "running":
        raise ValueError(
            "quantitative race strategy currently supports running only; "
            "cycling, swimming and triathlon need discipline-specific inputs"
        )
    values = (distance_km, target_finish_min, elevation_gain_m, timezone_shift_hours)
    if (
        any(not math.isfinite(value) for value in values)
        or distance_km <= 0
        or target_finish_min <= 0
        or elevation_gain_m < 0
        or not -14 <= timezone_shift_hours <= 14
    ):
        raise ValueError("race distance, elevation, or target time is invalid")
    if not event_name.strip() or len(event_name) > 200:
        raise ValueError("event name is invalid")
    if temperature_c is not None and not math.isfinite(temperature_c):
        raise ValueError("temperature is invalid")
    if wind_kph is not None and (not math.isfinite(wind_kph) or wind_kph < 0):
        raise ValueError("wind is invalid")
    event = RaceEvent(
        uuid4().hex,
        event_name.strip(),
        event_date,
        distance_km,
        elevation_gain_m,
        location.strip(),
        "unverified:user-input",
        datetime.now(UTC),
        "user-provided/unverified",
    )
    forecast = None
    if temperature_c is not None or wind_kph is not None:
        forecast = {"temperature_c": temperature_c or 0.0, "wind_kph": wind_kph or 0.0}
    strategy = RaceStrategyService().build(
        event=event,
        target_finish_min=target_finish_min,
        forecast=forecast,
        travel_context={"timezone_shift_hours": timezone_shift_hours},
    )
    result = asdict(strategy)
    result["event"]["event_date"] = event.event_date.isoformat()
    result["event"]["source_updated_at"] = event.source_updated_at.isoformat()
    gaps = [*strategy.data_gaps, "event details are user-provided and unverified"]
    if temperature_c is None and forecast is not None:
        gaps.append("temperature unavailable")
    if wind_kph is None and forecast is not None:
        gaps.append("wind unavailable")
    result["data_gaps"] = gaps
    return {"strategy": result, "saved": False}


def analyze_response_samples(
    *,
    stimulus_name: str,
    outcome_name: str,
    samples: list[tuple[float, float]],
) -> dict[str, object]:
    if not 3 <= len(samples) <= 100 or any(
        len(pair) != 2 or any(not math.isfinite(value) for value in pair) for pair in samples
    ):
        raise ValueError("provide 3 to 100 finite numeric sample pairs")
    return asdict(
        PersonalResponseLearner().analyze(
            stimulus_name=stimulus_name, outcome_name=outcome_name, samples=samples
        )
    )
