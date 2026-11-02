import unittest

from athlete_growth.adapters.running_form.contracts import MetricResult, SafetyDecision
from athlete_growth.adapters.running_form.scoring import score_running_form, score_total


def metric(key: str, value: float) -> MetricResult:
    return MetricResult(
        key=key,
        value=value,
        unit=None,
        visibility=0.9,
        confidence=0.9,
        evidence_frames=(0,),
    )


class RunningFormScoringTest(unittest.TestCase):
    def test_stable_upright_posture_is_not_penalized_for_missing_prescribed_lean(self) -> None:
        result = score_running_form(
            {"trunk_lean_deg": metric("trunk_lean_deg", 0)},
            usable_dimensions={"posture"},
        )

        self.assertEqual(result.dimension_scores["posture"], 100.0)

    def test_total_score_renormalizes_only_measurable_dimensions(self) -> None:
        total = score_total(
            {
                "posture": 80.0,
                "braking": 60.0,
                "symmetry": None,
                "coordination": None,
                "economy": 70.0,
            }
        )

        self.assertAlmostEqual(total, 69.23, places=2)

    def test_missing_dimensions_are_none_not_zero(self) -> None:
        result = score_running_form(
            {
                "trunk_lean_deg": metric("trunk_lean_deg", 10),
                "vertical_oscillation_ratio": metric(
                    "vertical_oscillation_ratio", 0.07
                ),
            },
            usable_dimensions={"posture", "economy"},
        )

        self.assertIsNone(result.dimension_scores["braking"])
        self.assertIsNone(result.dimension_scores["symmetry"])
        self.assertGreater(result.overall_score, 0)
        self.assertAlmostEqual(result.coverage, 0.4)

    def test_low_coverage_suppresses_overall_score(self) -> None:
        result = score_running_form(
            {"trunk_lean_deg": metric("trunk_lean_deg", 10)},
            usable_dimensions={"posture"},
        )

        self.assertIsNone(result.overall_score)
        self.assertLess(result.coverage, 0.4)

    def test_safety_mode_suppresses_an_otherwise_valid_total(self) -> None:
        result = score_running_form(
            {
                "trunk_lean_deg": metric("trunk_lean_deg", 10),
                "vertical_oscillation_ratio": metric(
                    "vertical_oscillation_ratio", 0.07
                ),
            },
            usable_dimensions={"posture", "economy"},
            safety_decision=SafetyDecision(True, ("worsening_pain",)),
        )

        self.assertIsNone(result.overall_score)


if __name__ == "__main__":
    unittest.main()
