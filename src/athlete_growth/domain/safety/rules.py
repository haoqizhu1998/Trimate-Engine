"""Non-medical deterministic training safety rules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from athlete_growth.domain.records import CanonicalRecord

DISCLAIMER = "General wellness guidance only; not medical diagnosis or treatment."


class RiskLevel(StrEnum):
    UNKNOWN = "unknown"
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class SafetyAssessment:
    level: RiskLevel
    reasons: tuple[str, ...]
    actions: tuple[str, ...]
    data_gaps: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    ruleset_version: str = "safety-rules/1.0"
    disclaimer: str = DISCLAIMER


class SafetyRules:
    def assess(
        self,
        *,
        check_in: CanonicalRecord | None,
        observations: list[CanonicalRecord],
        acute_load: float | None = None,
        chronic_load: float | None = None,
    ) -> SafetyAssessment:
        level = RiskLevel.LOW
        reasons: list[str] = []
        actions: list[str] = []
        gaps: list[str] = []
        evidence: list[str] = []

        if check_in is None:
            level = RiskLevel.UNKNOWN
            gaps.append("daily check-in")
            actions.append("Complete a check-in before relying on readiness guidance.")
        else:
            evidence.append(check_in.record_id)
            pain = self._number(check_in.data.get("pain"))
            fatigue = self._number(check_in.data.get("fatigue"))
            soreness = self._number(check_in.data.get("soreness"))
            illness = check_in.data.get("illness") is True
            if illness or pain >= 7:
                level = RiskLevel.HIGH
                reasons.append("illness signal or high pain score")
                actions.extend(
                    (
                        "Do not perform intense training today.",
                        "Seek appropriate healthcare professional guidance if symptoms "
                        "are concerning or persistent.",
                    )
                )
            elif pain >= 4 or fatigue >= 8 or soreness >= 8:
                level = RiskLevel.MODERATE
                reasons.append("subjective recovery signal is elevated")
                actions.append("Reduce intensity and reassess during the session.")

        if acute_load is not None and chronic_load is not None and chronic_load > 0:
            ratio = acute_load / chronic_load
            if ratio >= 1.8:
                level = RiskLevel.HIGH
                reasons.append("acute load is substantially above recent baseline")
                actions.append("Avoid adding load until recovery and baseline are reviewed.")
            elif ratio >= 1.5 and level not in {RiskLevel.HIGH}:
                level = RiskLevel.MODERATE
                reasons.append("acute load is above recent baseline")
                actions.append("Reduce planned load and prioritize recovery.")

        for observation in observations:
            evidence.append(observation.record_id)
            resting = self._optional_number(observation.data.get("resting_hr"))
            baseline = self._optional_number(observation.data.get("resting_hr_baseline"))
            if resting is not None and baseline and resting >= baseline * 1.1:
                if level in {RiskLevel.LOW, RiskLevel.UNKNOWN}:
                    level = RiskLevel.MODERATE
                reasons.append("resting heart rate is elevated versus baseline")
                actions.append("Prefer easy activity and monitor how you feel.")

        if level == RiskLevel.LOW and not actions:
            actions.append("Proceed within the approved plan and normal constraints.")
        return SafetyAssessment(
            level,
            tuple(dict.fromkeys(reasons)),
            tuple(dict.fromkeys(actions)),
            tuple(dict.fromkeys(gaps)),
            tuple(dict.fromkeys(evidence)),
        )

    @staticmethod
    def _number(value: object) -> float:
        return float(value) if isinstance(value, (int, float)) else 0.0

    @staticmethod
    def _optional_number(value: object) -> float | None:
        return float(value) if isinstance(value, (int, float)) else None
