"""General sports nutrition planning models."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NutritionProfile:
    weight_kg: float
    dietary_preferences: tuple[str, ...] = ()
    allergies: tuple[str, ...] = ()
    sweat_rate_ml_per_hour: int | None = None

    def __post_init__(self) -> None:
        if self.weight_kg <= 0:
            raise ValueError("weight_kg must be positive")


@dataclass(frozen=True, slots=True)
class FuelingContext:
    duration_min: int
    intensity: str
    is_race: bool
    temperature_c: float | None = None


@dataclass(frozen=True, slots=True)
class NutritionPlan:
    carbohydrate_g_per_hour: tuple[int, int]
    fluid_ml_per_hour: tuple[int, int]
    sodium_mg_per_hour: tuple[int, int]
    pre_session: str
    during_session: str
    post_session: str
    preferences_applied: tuple[str, ...]
    data_gaps: tuple[str, ...]
    disclaimer: str
