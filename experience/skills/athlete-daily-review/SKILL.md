---
name: athlete-daily-review
description: 用最新同步状态、日报周报和活动证据回答今天怎么练、训练变化与提醒；当用户要日常训练分析时启用。
---

# 日常训练复盘

读取 `get_sync_status`，再按问题读取 `get_today_report`、`get_weekly_report`、`list_activities` 或 `list_notifications`。对新提醒说明触发依据；对已过期或缺失数据说明可能影响，不把旧报告当作今日状态。

先讲最重要的训练状态与安全提示，再讲依据、数据缺口和下一步。主观 Check-in 不存在时把恢复准备度记为未知。发现疼痛或疾病信号时转入恢复 Skill；用户想把建议变成目标或计划时转入目标计划 Skill。

不要凭一次观测推断长期趋势，也不要把相关性说成因果。用户只问一个指标时保持回答聚焦。

HRV 问题读取日报 `hrv_context`，核对最近一晚日期、七天有效夜数和前 21 天参考夜数。只在两个窗口都充分时解释数值差异；这不是佳明个人基线，也不单独批准强度课。方法边界见 `../../docs/hrv-recovery-methodology.md`。
