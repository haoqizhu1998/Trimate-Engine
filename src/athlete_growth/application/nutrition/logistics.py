"""Check whether user-declared race fueling supplies and access can meet a rehearsed target."""

from __future__ import annotations

import math


def preview_fueling_logistics(
    *,
    duration_min: int,
    declared_tested_carbs_g_per_hour: float,
    carbs_per_serving_g: float,
    available_servings: int,
    opportunity_times_min: list[int],
) -> dict[str, object]:
    if type(duration_min) is not int or not 1 <= duration_min <= 1440:
        raise ValueError("fueling duration is invalid")
    if (
        isinstance(declared_tested_carbs_g_per_hour, bool)
        or not isinstance(declared_tested_carbs_g_per_hour, (int, float))
        or not math.isfinite(declared_tested_carbs_g_per_hour)
        or not 0 < declared_tested_carbs_g_per_hour <= 180
    ):
        raise ValueError("declared tested carb rate is invalid")
    if (
        isinstance(carbs_per_serving_g, bool)
        or not isinstance(carbs_per_serving_g, (int, float))
        or not math.isfinite(carbs_per_serving_g)
        or not 0 < carbs_per_serving_g <= 200
    ):
        raise ValueError("carbs per serving is invalid")
    if type(available_servings) is not int or not 0 <= available_servings <= 60:
        raise ValueError("available servings is invalid")
    if (
        len(opportunity_times_min) > 60
        or any(
            type(item) is not int or not 0 < item <= duration_min
            for item in opportunity_times_min
        )
        or opportunity_times_min != sorted(set(opportunity_times_min))
    ):
        raise ValueError("fueling opportunities are invalid")
    target_total = declared_tested_carbs_g_per_hour * duration_min / 60
    needed_servings = math.ceil(target_total / carbs_per_serving_g)
    accessible_servings = min(available_servings, len(opportunity_times_min))
    accessible_carbs = accessible_servings * carbs_per_serving_g
    return {
        "duration_min": duration_min,
        "target_source": "user-declared rehearsed rate; unverified",
        "declared_tested_carbs_g_per_hour": declared_tested_carbs_g_per_hour,
        "target_carbs_g": round(target_total, 1),
        "carbs_per_serving_g": carbs_per_serving_g,
        "servings_needed": needed_servings,
        "available_servings": available_servings,
        "opportunity_count": len(opportunity_times_min),
        "accessible_servings": accessible_servings,
        "accessible_carbs_g": round(accessible_carbs, 1),
        "meets_declared_target": accessible_servings >= needed_servings,
        "shortfall_g": round(max(0, target_total - accessible_carbs), 1),
        "data_gaps": [
            "Product label, actual availability and individual tolerance are not verified",
            "One serving per stated opportunity is assumed; water, sodium and timing "
            "are not assessed",
        ],
        "saved": False,
    }
