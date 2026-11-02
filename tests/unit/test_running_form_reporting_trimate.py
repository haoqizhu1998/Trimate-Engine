import unittest

from athlete_growth.adapters.running_form.reporting import (
    build_private_model_payload,
    render_running_form_report,
)


def payload(*, safety_mode: bool = False):
    return {
        "overall_score": 78.0,
        "dimension_scores": {
            "posture": 82.0,
            "braking": 68.0,
            "symmetry": None,
            "coordination": 80.0,
            "economy": 76.0,
        },
        "coverage": 0.8,
        "confidence": 0.86,
        "safety_mode": safety_mode,
        "safety_reasons": ["worsening_pain"] if safety_mode else [],
        "findings": [
            {
                "key": "braking",
                "title": "落地点略靠前",
                "evidence": "脚踝落点位于髋部投影前方",
                "severity": "improve",
            }
        ],
        "recommendations": [
            {
                "problem_key": "braking",
                "cue": "脚落在身体下面",
                "drills": ["原地快步 3组×20秒", "A Skip 3组×20米"],
                "frequency": "每周2次",
            }
        ],
        "limitations": ["单一侧面视角，左右对称未评估"],
        "history": {"comparable": False, "reasons": ["no_previous"]},
        "user_profile": {"nickname": "真实姓名"},
        "local_video_path": "/private/user/form.mp4",
    }


class RunningFormReportingTest(unittest.TestCase):
    def test_report_has_layered_result_and_actionable_adjustment(self) -> None:
        report = render_running_form_report(payload())

        for phrase in (
            "快速结论",
            "综合评分：78",
            "不等于伤病风险",
            "指标覆盖率：80%",
            "稳定与左右对称：未评估",
            "专业分析",
            "脚落在身体下面",
            "原地快步 3组×20秒",
            "复测建议",
            "单一侧面视角",
            "不使用固定 180 步频",
            "只有明确确认是同一位跑者时",
        ):
            self.assertIn(phrase, report)
        self.assertNotIn("你自己的", report)

    def test_safety_mode_suppresses_total_and_uses_conservative_advice(self) -> None:
        report = render_running_form_report(payload(safety_mode=True))

        self.assertNotIn("综合评分：78", report)
        self.assertIn("安全模式", report)
        self.assertIn("暂停强度训练", report)
        self.assertIn("专业人员", report)

    def test_model_payload_removes_identity_and_local_paths(self) -> None:
        private = build_private_model_payload(payload())
        serialized = str(private)

        self.assertNotIn("真实姓名", serialized)
        self.assertNotIn("/private/user/form.mp4", serialized)
        self.assertNotIn("user_profile", private)
        self.assertNotIn("history", private)
        self.assertEqual(private["overall_score"], 78.0)

    def test_model_receives_history_only_after_same_subject_confirmation(self) -> None:
        result = payload()
        result["same_subject_confirmed"] = True

        private = build_private_model_payload(result)

        self.assertEqual(private["history"], result["history"])


if __name__ == "__main__":
    unittest.main()
