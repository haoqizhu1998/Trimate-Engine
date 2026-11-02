"""Evidence-backed race strategy and offline export."""

from __future__ import annotations

import os
from pathlib import Path

from athlete_growth.domain.race import RaceEvent, RaceStrategy
from athlete_growth.domain.safety import DISCLAIMER


class RaceStrategyService:
    algorithm_version = "race-strategy/1.0"

    def build(
        self,
        *,
        event: RaceEvent,
        target_finish_min: float,
        forecast: dict[str, float] | None,
        travel_context: dict[str, float] | None = None,
    ) -> RaceStrategy:
        if target_finish_min <= 0:
            raise ValueError("target finish time must be positive")
        base_pace = target_finish_min / event.distance_km
        elevation_adjustment = min(0.08, event.elevation_gain_m / 10000)
        pace = (
            round(base_pace * (1 - 0.02), 2),
            round(base_pace * (1 + elevation_adjustment), 2),
        )
        gaps: list[str] = []
        risks: list[str] = []
        if forecast is None:
            gaps.append("weather forecast unavailable")
            risks.append("Recheck heat, wind and precipitation within 48 hours of the race.")
        else:
            if forecast.get("temperature_c", 0) >= 28:
                risks.append("Heat may require a slower start and additional hydration planning.")
            if forecast.get("wind_kph", 0) >= 25:
                risks.append("Strong wind may make even pacing by effort preferable.")
        logistics = ["Confirm transport, bib collection and start-area timing."]
        shift = (travel_context or {}).get("timezone_shift_hours", 0)
        if abs(shift) >= 2:
            logistics.append("Allow adaptation time for the reported timezone shift.")
        fueling = (
            (
                "Use only rehearsed fuel; consider a long-race carbohydrate plan "
                "that matches the athlete's tested tolerance.",
                "Carry or identify fluid access points before race day.",
            )
            if target_finish_min >= 150
            else (
                "Use a familiar pre-race meal; short races do not inherit a "
                "long-race hourly carbohydrate target.",
                "Confirm whether fluid access is needed for the expected duration and conditions.",
            )
        )
        return RaceStrategy(
            event,
            target_finish_min,
            pace,
            (
                "Start conservatively and use effort on climbs.",
                "Review pace after halfway; do not force the target if safety signals change.",
            ),
            fueling,
            ("Use tested shoes, clothing and weather-appropriate layers.",),
            tuple(logistics),
            tuple(risks),
            (
                event.source_url,
                event.source_updated_at.isoformat(),
                event.license,
                self.algorithm_version,
            ),
            tuple(gaps),
            DISCLAIMER,
        )

    def export_markdown(self, strategy: RaceStrategy, destination: Path) -> Path:
        event = strategy.event
        lines = [
            f"# {event.name} Race Strategy",
            "",
            f"- Date: {event.event_date.isoformat()}",
            f"- Distance: {event.distance_km:g} km",
            f"- Elevation gain: {event.elevation_gain_m:g} m",
            f"- Target finish: {strategy.target_finish_min:g} minutes",
            (
                "- Target pace range: "
                f"{strategy.target_pace_min_per_km[0]:.2f}–"
                f"{strategy.target_pace_min_per_km[1]:.2f} min/km"
            ),
            "",
            "## Pacing",
            *[f"- {item}" for item in strategy.pacing],
            "",
            "## Fueling",
            *[f"- {item}" for item in strategy.fueling],
            "",
            "## Equipment and logistics",
            *[f"- {item}" for item in strategy.equipment + strategy.logistics],
            "",
            "## Risks and data gaps",
            *[f"- {item}" for item in strategy.risks + strategy.data_gaps],
            "",
            "## Evidence and authorization",
            *[f"- {item}" for item in strategy.evidence],
            "",
            f"> {strategy.disclaimer}",
        ]
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.chmod(destination, 0o600)
        return destination
