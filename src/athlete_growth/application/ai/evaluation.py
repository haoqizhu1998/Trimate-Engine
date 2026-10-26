"""Fixed evaluation cases for model safety and factual grounding."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from athlete_growth.ports.model_provider import ModelResponse


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    case_id: str
    required_phrases: tuple[str, ...]
    forbidden_phrases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    case_id: str
    passed: bool
    failures: tuple[str, ...]


def load_cases(path: Path) -> tuple[EvaluationCase, ...]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, list):
        raise ValueError("AI evaluation fixture must be a list")
    cases: list[EvaluationCase] = []
    for row in document:
        if not isinstance(row, dict):
            raise ValueError("AI evaluation case must be an object")
        required = row.get("required_phrases", [])
        forbidden = row.get("forbidden_phrases", [])
        if not isinstance(required, list) or not isinstance(forbidden, list):
            raise ValueError("evaluation phrases must be lists")
        cases.append(
            EvaluationCase(
                str(row["case_id"]),
                tuple(str(value) for value in required),
                tuple(str(value) for value in forbidden),
            )
        )
    return tuple(cases)


def evaluate(case: EvaluationCase, response: ModelResponse) -> EvaluationResult:
    lowered = response.text.lower()
    failures = [
        f"missing required phrase: {phrase}"
        for phrase in case.required_phrases
        if phrase.lower() not in lowered
    ]
    failures.extend(
        f"contains forbidden phrase: {phrase}"
        for phrase in case.forbidden_phrases
        if phrase.lower() in lowered
    )
    return EvaluationResult(case.case_id, not failures, tuple(failures))
