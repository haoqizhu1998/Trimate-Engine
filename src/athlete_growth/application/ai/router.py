"""Privacy-aware, cost-bounded model routing with offline fallback."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from athlete_growth.ports.model_provider import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
)

PrivacyMode = Literal["local_only", "minimized", "standard"]


@dataclass(frozen=True, slots=True)
class ModelConsent:
    external_ai: bool


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    max_cost: Decimal
    privacy_mode: PrivacyMode


@dataclass(frozen=True, slots=True)
class ModelRun:
    provider: str
    model: str
    prompt_version: str
    occurred_at: datetime
    evidence_ids: tuple[str, ...]
    estimated_cost: Decimal
    safety_overridden: bool


class OfflineRulesProvider:
    def capabilities(self):
        from athlete_growth.ports.model_provider import ModelCapabilities

        return ModelCapabilities("offline-rules", False, True, ("text",), 8192)

    def estimate_cost(self, request: ModelRequest) -> Decimal:
        return Decimal("0")

    def generate(self, request: ModelRequest) -> ModelResponse:
        if request.risk_level == "high":
            text = (
                "Safety override: avoid intense training and seek appropriate professional "
                "guidance when symptoms are concerning."
            )
        elif not request.evidence_ids:
            text = "Evidence is incomplete, so the conclusion is uncertain."
        else:
            text = "Offline explanation generated from deterministic metrics and cited evidence."
        return ModelResponse(
            "offline-rules",
            "deterministic",
            text,
            {},
            request.evidence_ids,
            "deterministic fallback",
            Decimal("0"),
        )

    def healthcheck(self) -> bool:
        return True


class ModelRouter:
    def __init__(self, providers: list[ModelProvider], *, audit=None) -> None:
        self.providers = providers
        self.offline = OfflineRulesProvider()
        self.audit = audit

    def generate(
        self,
        request: ModelRequest,
        *,
        consent: ModelConsent,
        policy: RoutingPolicy,
    ) -> ModelResponse:
        response: ModelResponse | None = None
        for provider in self.providers:
            capabilities = provider.capabilities()
            if capabilities.external and (
                not consent.external_ai or policy.privacy_mode == "local_only"
            ):
                continue
            if provider.estimate_cost(request) > policy.max_cost:
                continue
            try:
                response = provider.generate(self._minimized(request, policy))
                break
            except (ConnectionError, TimeoutError, OSError, ValueError):
                continue
        if response is None:
            response = self.offline.generate(request)
        overridden = False
        if request.risk_level == "high" and response.provider != "offline-rules":
            response = self.offline.generate(request)
            overridden = True
        allowed_evidence = tuple(
            evidence for evidence in response.evidence_ids if evidence in request.evidence_ids
        )
        if allowed_evidence != response.evidence_ids:
            response = ModelResponse(
                response.provider,
                response.model,
                response.text,
                response.data,
                allowed_evidence,
                "some model citations were not authorized",
                response.estimated_cost,
            )
        run = ModelRun(
            response.provider,
            response.model,
            request.prompt_version,
            datetime.now(UTC),
            response.evidence_ids,
            response.estimated_cost,
            overridden,
        )
        if self.audit is not None:
            self.audit(run)
        return response

    @staticmethod
    def _minimized(request: ModelRequest, policy: RoutingPolicy) -> ModelRequest:
        if policy.privacy_mode != "minimized":
            return request
        return ModelRequest(
            system=request.system,
            user=request.user,
            evidence_ids=request.evidence_ids,
            risk_level=request.risk_level,
            structured_schema=request.structured_schema,
            max_output_tokens=min(request.max_output_tokens, 800),
            prompt_version=request.prompt_version,
        )
