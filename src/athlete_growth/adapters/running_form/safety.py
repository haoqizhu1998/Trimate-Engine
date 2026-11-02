"""Conservative safety-mode decisions for running-form analysis."""

from __future__ import annotations

from athlete_growth.adapters.running_form.contracts import MetricResult, SafetyDecision

WORSENING_PAIN_PHRASES = (
    "疼痛加重",
    "越来越痛",
    "越跑越痛",
    "无法承重",
    "不能承重",
)


def evaluate_safety(
    metrics: dict[str, MetricResult],
    *,
    user_note: str = "",
) -> SafetyDecision:
    reasons: list[str] = []
    asymmetry = metrics.get("left_right_asymmetry")
    if (
        asymmetry is not None
        and asymmetry.value is not None
        and asymmetry.confidence >= 0.7
        and len(asymmetry.evidence_frames) >= 3
        and asymmetry.value >= 0.35
    ):
        reasons.append("marked_limp_or_asymmetry")
    if any(phrase in user_note for phrase in WORSENING_PAIN_PHRASES):
        reasons.append("worsening_pain")
    return SafetyDecision(safety_mode=bool(reasons), reasons=tuple(reasons))
