---
name: athlete-decision-coach
description: 依据当天证据预览训练选择、记录执行反馈，并在用户确认后启用有边界的个体化适应；用户询问今天如何调整或希望 AI 逐渐了解自己时启用。
---

# 训练决策与个体化适应

先读 `get_today_report` 和 `get_growth_profile`，再用 `preview_training_decision` 取得当天允许的选项、建议、依据和数据缺口。把“系统推荐”与“用户可选”分开说；缺少 Check-in 时准备度未知，任何已批准的个人偏好也不能覆盖高风险安全结果。用户看到预览并明确选定选项后，才调用 `confirm_training_decision`。确认只保存决策日志，不自动改写训练计划。

训练结束后，只有用户提供实际完成情况时才调用 `record_decision_outcome`。`effect` 表示用户认为结果相对预期是更好、相近或更差；`pain_after` 是用户报告的运动后疼痛程度。修改旧反馈时先从 `list_training_decisions` 读取最新 revision。不得把设备记录自动冒充用户主观反馈。

`get_growth_profile` 可能提出候选适应。向用户展示样本数、依据、适用范围和“相关不等于因果”的限制；只有用户明确批准候选 ID 后才调用 `confirm_growth_adaptation`。已启用的适应只影响低风险日的推荐选项，不改变安全规则、目标或已确认计划。用户要求关闭时调用 `revoke_growth_adaptation`，保留历史。不要声称模型权重已自我训练或已证明某方案对用户有效。
