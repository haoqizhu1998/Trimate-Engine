"""Training-day and race-day nutrition planning."""

from __future__ import annotations

from athlete_growth.domain.nutrition import (
    FuelingContext,
    NutritionPlan,
    NutritionProfile,
)

NUTRITION_DISCLAIMER = (
    "General sports nutrition guidance only; individual medical or dietary needs require "
    "a qualified professional."
)


class NutritionPlanner:
    def plan(self, profile: NutritionProfile, context: FuelingContext) -> NutritionPlan:
        if context.duration_min <= 60:
            carbs = (0, 0)
        elif context.duration_min >= 150:
            carbs = (60, 90)
        elif context.duration_min >= 90:
            carbs = (30, 60)
        else:
            carbs = (0, 30)
        fluid = (
            (profile.sweat_rate_ml_per_hour, profile.sweat_rate_ml_per_hour)
            if profile.sweat_rate_ml_per_hour
            else (400, 800)
        )
        sodium = (300, 600) if context.duration_min >= 90 else (0, 400)
        data_gaps: list[str] = []
        if profile.sweat_rate_ml_per_hour is None:
            data_gaps.append("personal sweat rate")
        if context.temperature_c is None:
            data_gaps.append("temperature")
        elif context.temperature_c >= 28:
            data_gaps.append(
                "heat: fluid needs and tolerance require individual assessment; "
                "temperature alone does not justify a higher fixed intake"
            )
        pre = (
            "Race day: use a rehearsed carbohydrate-rich meal 2–4 hours before start."
            if context.is_race
            else "Training day: use a familiar meal and arrive normally hydrated."
        )
        during = (
            "Practice the stated carbohydrate and fluid ranges; avoid overdrinking "
            "and do not introduce new products on race day."
            if carbs[1] > 0
            else "Water to thirst is generally sufficient for this short easy session."
        )
        post = (
            f"Include carbohydrate and roughly {round(profile.weight_kg * 0.3)} g protein "
            "in a normal recovery meal."
        )
        return NutritionPlan(
            carbs,
            fluid,
            sodium,
            pre,
            during,
            post,
            profile.dietary_preferences,
            tuple(data_gaps),
            NUTRITION_DISCLAIMER,
        )
