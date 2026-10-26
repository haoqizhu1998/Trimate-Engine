from __future__ import annotations

import unittest

from athlete_growth.adapters.ai import providers
from athlete_growth.ports.model_provider import ModelRequest


class RecordingTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, url, headers, payload):
        self.calls.append((url, headers, payload))
        return self.response


class ModelProviderContractTests(unittest.TestCase):
    def test_openai_compatible_provider_uses_chat_completions_contract(self) -> None:
        provider_type = getattr(providers, "OpenAICompatibleProvider", None)
        self.assertIsNotNone(provider_type)
        transport = RecordingTransport({"choices": [{"message": {"content": "explanation"}}]})
        provider = provider_type(
            name="local",
            base_url="http://127.0.0.1:11434",
            model="local-model",
            api_key=None,
            external=False,
            transport=transport,
        )

        response = provider.generate(ModelRequest("system", "user", ("e1",), "low"))

        self.assertEqual(response.text, "explanation")
        self.assertEqual(response.provider, "local")
        self.assertTrue(transport.calls[0][0].endswith("/v1/chat/completions"))

    def test_anthropic_and_gemini_adapters_parse_native_responses(self) -> None:
        anthropic_transport = RecordingTransport(
            {"content": [{"type": "text", "text": "anthropic answer"}]}
        )
        gemini_transport = RecordingTransport(
            {"candidates": [{"content": {"parts": [{"text": "gemini answer"}]}}]}
        )
        request = ModelRequest("system", "user", ("e1",), "low")

        anthropic_provider = providers.AnthropicProvider(
            api_key="test-key", model="claude-test", transport=anthropic_transport
        )
        anthropic = anthropic_provider.generate(request)
        gemini = providers.GeminiProvider(
            api_key="test-key", model="gemini-test", transport=gemini_transport
        ).generate(request)

        self.assertEqual(anthropic.text, "anthropic answer")
        self.assertEqual(gemini.text, "gemini answer")
        self.assertNotIn("test-key", repr(anthropic_provider))


if __name__ == "__main__":
    unittest.main()
