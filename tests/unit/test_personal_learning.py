from __future__ import annotations

import unittest

from athlete_growth.application.learn import service


class PersonalLearningTests(unittest.TestCase):
    def test_response_insight_is_explicitly_correlation_not_causation(self) -> None:
        learner_type = getattr(service, "PersonalResponseLearner", None)
        self.assertIsNotNone(learner_type)
        insight = learner_type().analyze(
            stimulus_name="weekly load",
            outcome_name="next-day fatigue",
            samples=[(100, 3), (200, 5), (300, 8), (150, 4)],
        )

        self.assertEqual(insight.sample_size, 4)
        self.assertGreater(insight.correlation, 0)
        self.assertIn("association", insight.statement.lower())
        self.assertIn("does not establish causation", insight.statement.lower())


if __name__ == "__main__":
    unittest.main()
