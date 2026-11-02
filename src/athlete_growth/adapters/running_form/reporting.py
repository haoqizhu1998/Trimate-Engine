"""Deterministic, privacy-safe reporting for running-form assessments."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

DIMENSION_LABELS = {
    "posture": "姿态控制",
    "braking": "落地与制动",
    "symmetry": "稳定与左右对称",
    "coordination": "动作协调性",
    "economy": "跑步经济性倾向",
}

MODEL_PAYLOAD_KEYS = {
    "overall_score",
    "dimension_scores",
    "coverage",
    "confidence",
    "safety_mode",
    "safety_reasons",
    "findings",
    "recommendations",
    "limitations",
    "history",
    "viewpoints",
    "metrics",
    "unmeasured_metrics",
}


def build_private_model_payload(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return only analysis facts that are safe to send to a text model."""

    return {
        key: deepcopy(value)
        for key, value in result.items()
        if key in MODEL_PAYLOAD_KEYS
        and (key != "history" or result.get("same_subject_confirmed") is True)
    }


def _percent(value: Any) -> str:
    try:
        return f"{round(float(value) * 100)}%"
    except (TypeError, ValueError):
        return "未评估"


def _score(value: Any) -> str:
    if value is None:
        return "未评估"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "未评估"
    return str(round(number))


def render_running_form_report(result: Mapping[str, Any]) -> str:
    """Render a stable Chinese report without inventing or changing scores."""

    safety_mode = bool(result.get("safety_mode"))
    dimensions = result.get("dimension_scores") or {}
    lines = ["## 快速结论"]

    if safety_mode:
        lines.extend(
            [
                "**安全模式已启用**：本次不展示综合评分，也不据此追求动作优化。",
                "先暂停强度训练；如疼痛持续、加重或已经改变动作，请咨询运动医学或康复专业人员。",
            ]
        )
    elif result.get("overall_score") is not None:
        lines.append(f"- 综合评分：{_score(result.get('overall_score'))}/100")
        lines.append("- 评分含义：只表示本次视频中可见动作与规则基线的匹配度，不等于伤病风险。")

    lines.extend(
        [
            f"- 指标覆盖率：{_percent(result.get('coverage'))}",
            f"- 分析置信度：{_percent(result.get('confidence'))}",
            "",
            "### 五项评分",
        ]
    )
    for key, label in DIMENSION_LABELS.items():
        lines.append(f"- {label}：{_score(dimensions.get(key))}")

    findings = list(result.get("findings") or [])[:3]
    lines.extend(["", "## 专业分析（可见动作观察）"])
    if findings:
        for finding in findings:
            title = finding.get("title") or "动作观察"
            evidence = finding.get("evidence") or ""
            lines.append(f"- **{title}**：{evidence}" if evidence else f"- **{title}**")
    else:
        lines.append("- 当前视频没有形成足够可靠的动作结论。")

    recommendations = list(result.get("recommendations") or [])[:3]
    lines.extend(["", "## 调整方案"])
    if safety_mode:
        lines.append("安全问题优先，暂不安排会增加疼痛或代偿的专项练习。")
    elif recommendations:
        for index, recommendation in enumerate(recommendations, start=1):
            lines.append(f"### {index}. {recommendation.get('problem_key') or '动作调整'}")
            if recommendation.get("cue"):
                lines.append(f"- 跑步口令：{recommendation['cue']}")
            drills = recommendation.get("drills") or []
            if drills:
                lines.append(f"- 专项练习：{'；'.join(str(item) for item in drills)}")
            if recommendation.get("frequency"):
                lines.append(f"- 频率：{recommendation['frequency']}")
            if recommendation.get("caution"):
                lines.append(f"- 调整边界：{recommendation['caution']}")
    else:
        lines.append("- 暂无可靠动作建议，建议补充更清晰、完整的跑步视频后复测。")

    lines.extend(
        [
            "",
            "## 复测建议",
            "2–4 周后用相近速度复测：相机固定、全身与双脚完整入镜；侧面看落地点与制动，正面或后方看左右稳定。只有明确确认是同一位跑者时，才比较同条件变化。",
        ]
    )

    limitations = list(result.get("limitations") or [])
    if limitations:
        lines.extend(["", "## 分析限制"])
        lines.extend(f"- {item}" for item in limitations)

    lines.extend(
        [
            "",
            "> 不使用固定 180 步频或某一种着地方式作为统一答案；本报告用于跑步动作观察和训练建议，不做医学诊断。",
        ]
    )
    return "\n".join(lines)
