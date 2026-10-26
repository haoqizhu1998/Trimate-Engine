from __future__ import annotations

import unittest
from decimal import Decimal

from athlete_growth.application.ai import router
from athlete_growth.ports import model_provider


class FakeProvider:
    def __init__(self, name: str, *, external: bool, cost: str, text: str = "grounded"):
        self.name = name
        self.external = external
        self.cost = Decimal(cost)
        self.text = text
        self.calls = 0

    def capabilities(self):
        return model_provider.ModelCapabilities(self.name, self.external, True, ("text",), 8192)

    def estimate_cost(self, request):
        return self.cost

    def generate(self, request):
        self.calls += 1
        return model_provider.ModelResponse(
            provider=self.name,
            model="fake",
            text=self.text,
            data={},
            evidence_ids=request.evidence_ids,
            uncertainty="low",
            estimated_cost=self.cost,
        )

    def healthcheck(self):
        return True


class ModelRouterTests(unittest.TestCase):
    def test_without_external_consent_uses_offline_rules(self) -> None:
        router_type = getattr(router, "ModelRouter", None)
        consent_type = getattr(router, "ModelConsent", None)
        policy_type = getattr(router, "RoutingPolicy", None)
        request_type = getattr(model_provider, "ModelRequest", None)
        self.assertIsNotNone(router_type)
        external = FakeProvider("cloud", external=True, cost="0.01")
        engine = router_type([external])
        request = request_type(
            system="Explain without diagnosis.",
            user="How is training going?",
            evidence_ids=("e1",),
            risk_level="low",
        )

        response = engine.generate(
            request,
            consent=consent_type(external_ai=False),
            policy=policy_type(max_cost=Decimal("1"), privacy_mode="local_only"),
        )

        self.assertEqual(response.provider, "offline-rules")
        self.assertEqual(external.calls, 0)

    def test_cost_cap_skips_expensive_provider_and_uses_local(self) -> None:
        router_type = getattr(router, "ModelRouter", None)
        self.assertIsNotNone(router_type)
        expensive = FakeProvider("expensive", external=True, cost="5")
        local = FakeProvider("local", external=False, cost="0")
        request = model_provider.ModelRequest("system", "user", ("e1",), "low")

        response = router_type([expensive, local]).generate(
            request,
            consent=router.ModelConsent(external_ai=True),
            policy=router.RoutingPolicy(Decimal("1"), "minimized"),
        )

        self.assertEqual(response.provider, "local")
        self.assertEqual(expensive.calls, 0)
        self.assertEqual(local.calls, 1)

    def test_high_risk_output_is_overridden_by_deterministic_guard(self) -> None:
        unsafe = FakeProvider(
            "cloud", external=True, cost="0.01", text="Do a hard interval session today."
        )
        request = model_provider.ModelRequest("system", "user", ("e1",), "high")

        response = router.ModelRouter([unsafe]).generate(
            request,
            consent=router.ModelConsent(external_ai=True),
            policy=router.RoutingPolicy(Decimal("1"), "minimized"),
        )

        self.assertIn("professional", response.text.lower())
        self.assertIn("safety override", response.text.lower())


if __name__ == "__main__":
    unittest.main()
