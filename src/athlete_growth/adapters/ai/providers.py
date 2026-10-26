"""OpenAI, Anthropic, Gemini and OpenAI-compatible providers."""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any
from urllib.request import Request, urlopen

from athlete_growth.ports.model_provider import (
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
)

JsonTransport = Callable[[str, dict[str, str], dict[str, object]], dict[str, object]]


def _http_post(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urlopen(request, timeout=45) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("model response must be a JSON object")
    return result


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        model: str,
        api_key: str | None,
        external: bool,
        transport: JsonTransport = _http_post,
        input_cost_per_million: Decimal = Decimal("0"),
        output_cost_per_million: Decimal = Decimal("0"),
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self.external = external
        self._transport = transport
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(name={self.name!r}, base_url={self.base_url!r}, "
            f"model={self.model!r}, external={self.external!r})"
        )

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(self.name, self.external, True, ("text",), 128000)

    def estimate_cost(self, request: ModelRequest) -> Decimal:
        input_tokens = Decimal(len(request.system + request.user)) / Decimal(4)
        output_tokens = Decimal(request.max_output_tokens)
        return (
            input_tokens * self.input_cost_per_million
            + output_tokens * self.output_cost_per_million
        ) / Decimal(1_000_000)

    def generate(self, request: ModelRequest) -> ModelResponse:
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "max_tokens": request.max_output_tokens,
        }
        if request.structured_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "athlete_growth_response",
                    "schema": request.structured_schema,
                },
            }
        document = self._transport(f"{self.base_url}/v1/chat/completions", headers, payload)
        choices = document.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("OpenAI-compatible response has no choices")
        message = choices[0].get("message", {})
        text = str(message.get("content", ""))
        return self._response(request, text)

    def healthcheck(self) -> bool:
        return True

    def _response(self, request: ModelRequest, text: str) -> ModelResponse:
        data: dict[str, Any] = {}
        if request.structured_schema is not None:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    data = parsed
            except json.JSONDecodeError:
                pass
        return ModelResponse(
            self.name,
            self.model,
            text,
            data,
            request.evidence_ids,
            "model-generated; verify against cited evidence",
            self.estimate_cost(request),
        )


class OpenAIProvider(OpenAICompatibleProvider):
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-5-mini",
        transport: JsonTransport = _http_post,
    ) -> None:
        super().__init__(
            name="openai",
            base_url="https://api.openai.com",
            model=model,
            api_key=api_key,
            external=True,
            transport=transport,
        )


class AnthropicProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        transport: JsonTransport = _http_post,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._transport = transport

    def __repr__(self) -> str:
        return f"AnthropicProvider(model={self.model!r})"

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities("anthropic", True, True, ("text",), 200000)

    def estimate_cost(self, request: ModelRequest) -> Decimal:
        return Decimal("0")

    def generate(self, request: ModelRequest) -> ModelResponse:
        document = self._transport(
            "https://api.anthropic.com/v1/messages",
            {
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
            },
            {
                "model": self.model,
                "system": request.system,
                "messages": [{"role": "user", "content": request.user}],
                "max_tokens": request.max_output_tokens,
            },
        )
        content = document.get("content")
        if not isinstance(content, list) or not content:
            raise ValueError("Anthropic response has no content")
        text = str(content[0].get("text", ""))
        return ModelResponse(
            "anthropic",
            self.model,
            text,
            {},
            request.evidence_ids,
            "model-generated; verify against cited evidence",
            self.estimate_cost(request),
        )

    def healthcheck(self) -> bool:
        return True


class GeminiProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        transport: JsonTransport = _http_post,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._transport = transport

    def __repr__(self) -> str:
        return f"GeminiProvider(model={self.model!r})"

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities("gemini", True, True, ("text",), 1000000)

    def estimate_cost(self, request: ModelRequest) -> Decimal:
        return Decimal("0")

    def generate(self, request: ModelRequest) -> ModelResponse:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self._api_key}"
        )
        document = self._transport(
            url,
            {},
            {
                "systemInstruction": {"parts": [{"text": request.system}]},
                "contents": [{"role": "user", "parts": [{"text": request.user}]}],
                "generationConfig": {"maxOutputTokens": request.max_output_tokens},
            },
        )
        candidates = document.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("Gemini response has no candidates")
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(str(part.get("text", "")) for part in parts)
        return ModelResponse(
            "gemini",
            self.model,
            text,
            {},
            request.evidence_ids,
            "model-generated; verify against cited evidence",
            self.estimate_cost(request),
        )

    def healthcheck(self) -> bool:
        return True
