from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path

from athlete_growth.application.ai.evaluation import evaluate, load_cases
from athlete_growth.ports.model_provider import ModelResponse

FIXTURE = Path(__file__).parents[1] / "fixtures" / "ai_evaluation_cases.json"


class AIEvaluationTests(unittest.TestCase):
    def test_fixed_suite_covers_required_failure_modes(self) -> None:
        cases = load_cases(FIXTURE)

        self.assertEqual(
            {case.case_id for case in cases},
            {"factual-grounding", "overtraining", "user-correction", "medical-boundary"},
        )

    def test_evaluator_reports_forbidden_medical_claim(self) -> None:
        case = next(case for case in load_cases(FIXTURE) if case.case_id == "medical-boundary")
        response = ModelResponse(
            "fake",
            "fake",
            "This is a diagnosis and you need professional review.",
            {},
            (),
            "test",
            Decimal("0"),
        )

        result = evaluate(case, response)

        self.assertFalse(result.passed)
        self.assertTrue(any("diagnosis" in failure for failure in result.failures))


if __name__ == "__main__":
    unittest.main()
