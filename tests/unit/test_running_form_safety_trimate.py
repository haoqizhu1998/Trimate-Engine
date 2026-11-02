import unittest

from athlete_growth.adapters.running_form.contracts import MetricResult
from athlete_growth.adapters.running_form.safety import evaluate_safety


def asymmetry(value: float, confidence: float = 0.9) -> MetricResult:
    return MetricResult(
        key="left_right_asymmetry",
        value=value,
        unit="ratio",
        visibility=0.9,
        confidence=confidence,
        evidence_frames=(100, 200, 300),
    )


class RunningFormSafetyTest(unittest.TestCase):
    def test_persistent_marked_asymmetry_triggers_safety_mode(self) -> None:
        decision = evaluate_safety(
            {"left_right_asymmetry": asymmetry(0.42)}
        )

        self.assertTrue(decision.safety_mode)
        self.assertIn("marked_limp_or_asymmetry", decision.reasons)

    def test_worsening_pain_text_triggers_safety_mode(self) -> None:
        decision = evaluate_safety({}, user_note="跑着跑着膝盖越来越痛")

        self.assertTrue(decision.safety_mode)
        self.assertIn("worsening_pain", decision.reasons)

    def test_low_confidence_noise_and_foot_strike_type_do_not_trigger(self) -> None:
        decision = evaluate_safety(
            {
                "left_right_asymmetry": asymmetry(0.5, confidence=0.2),
                "foot_strike_offset": MetricResult(
                    key="foot_strike_offset",
                    value=0.15,
                    unit="body_ratio",
                    visibility=0.9,
                    confidence=0.9,
                ),
            },
            user_note="没有疼痛",
        )

        self.assertFalse(decision.safety_mode)


if __name__ == "__main__":
    unittest.main()
