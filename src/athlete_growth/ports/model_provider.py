"""Model provider contracts."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from athlete_growth.domain.records import JsonValue


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    name: str
    external: bool
    structured_output: bool
    modalities: tuple[str, ...]
    context_limit: int


@dataclass(frozen=True, slots=True)
class ModelRequest:
    system: str
    user: str
    evidence_ids: tuple[str, ...]
    risk_level: str
    structured_schema: dict[str, JsonValue] | None = None
    max_output_tokens: int = 800
    prompt_version: str = "coach/1.0"


@dataclass(frozen=True, slots=True)
class ModelResponse:
    provider: str
    model: str
    text: str
    data: dict[str, JsonValue]
    evidence_ids: tuple[str, ...]
    uncertainty: str
    estimated_cost: Decimal


class ModelProvider(Protocol):
    def capabilities(self) -> ModelCapabilities: ...

    def generate(self, request: ModelRequest) -> ModelResponse: ...

    def estimate_cost(self, request: ModelRequest) -> Decimal: ...

    def healthcheck(self) -> bool: ...
