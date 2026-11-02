"""Versioned deterministic scoring with visibility-aware normalization."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean

from athlete_growth.adapters.running_form.contracts import MetricResult, SafetyDecision
from athlete_growth.adapters.running_form.rules import (
    DIMENSION_METRICS,
    DIMENSION_WEIGHTS,
    METRIC_TARGETS,
    MIN_TOTAL_COVERAGE,
    RULE_VERSION,
)


@dataclass(frozen=True)
class ScoringResult:
    rule_version: str
    dimension_scores: dict[str, float | None]
    overall_score: float | None
    coverage: float
    confidence: float


def _metric_score(key: str, value: float) -> float:
    low, high, failure = METRIC_TARGETS[key]
    value = abs(value)
    if low <= value <= high:
        return 100.0
    distance = low - value if value < low else value - high
    allowed = max(0.001, failure - high)
    return round(max(0.0, 100.0 * (1.0 - distance / allowed)), 2)


def score_total(scores: dict[str, float | None]) -> float | None:
    weighted = [
        (score, DIMENSION_WEIGHTS[key])
        for key, score in scores.items()
        if score is not None and key in DIMENSION_WEIGHTS
    ]
    if not weighted:
        return None
    numerator = sum(score * weight for score, weight in weighted)
    denominator = sum(weight for _, weight in weighted)
    return round(numerator / denominator, 2)


def score_running_form(
    metrics: dict[str, MetricResult],
    *,
    usable_dimensions: set[str] | None = None,
    safety_decision: SafetyDecision | None = None,
) -> ScoringResult:
    allowed = usable_dimensions or set(DIMENSION_WEIGHTS)
    dimension_scores: dict[str, float | None] = {}
    confidences: list[float] = []
    for dimension, metric_keys in DIMENSION_METRICS.items():
        if dimension not in allowed:
            dimension_scores[dimension] = None
            continue
        measured = [
            metrics[key]
            for key in metric_keys
            if key in metrics and metrics[key].value is not None
        ]
        if not measured:
            dimension_scores[dimension] = None
            continue
        dimension_scores[dimension] = round(
            fmean(_metric_score(item.key, float(item.value or 0)) for item in measured),
            2,
        )
        confidences.extend(item.confidence for item in measured)

    measured_weight = sum(
        DIMENSION_WEIGHTS[key]
        for key, score in dimension_scores.items()
        if score is not None
    )
    coverage = round(measured_weight / sum(DIMENSION_WEIGHTS.values()), 3)
    overall = score_total(dimension_scores)
    if coverage < MIN_TOTAL_COVERAGE or (
        safety_decision is not None and safety_decision.safety_mode
    ):
        overall = None
    return ScoringResult(
        rule_version=RULE_VERSION,
        dimension_scores=dimension_scores,
        overall_score=overall,
        coverage=coverage,
        confidence=round(fmean(confidences), 3) if confidences else 0.0,
    )
