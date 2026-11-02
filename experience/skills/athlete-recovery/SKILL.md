---
name: athlete-recovery
description: 处理疲劳、疼痛、疾病后恢复与主观 Check-in；当用户讨论身体状态或复训时启用。
---

# 恢复与身体反馈

先读 `get_today_report` 的风险与缺口；按用户实际症状收集部位、起始时间、疼痛程度、肿胀、日常功能和稳定天数，再用 `assess_return_to_training` 查看非诊断性复训评估。不要自行判定伤病名称、痊愈或治疗方案；红旗或功能受限时尊重产品安全结果并建议专业评估。

记录 Check-in 时收集工具必需字段，调用 `preview_check_in` 展示精确字段；只有用户明确同意该次预览，才以原字段和预览摘要调用 `confirm_check_in`。未填写的 Check-in 不是零疲劳、零疼痛或可安全加量。

用户只是想讨论状态时，无需保存 Check-in。输出包含当前已知情况、未知情况、可做的保守下一步。
