"""Explainable analytics result models."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    metrics: dict[str, float | int | None]
    evidence_ids: tuple[str, ...]
    data_gaps: tuple[str, ...]
    confidence: float
    algorithm_version: str
