# 三途 Skill 原文迁移

本仓库 `experience/skills/` 中的 `body-assessment`、`endurance-athlete-assessment`、`injury-risk`、`periodic-training-summary`、`running-form-analysis`、`training-load`、`training-memory`、`training-nutrition-coach`、`training-plan`、`weekly-review`、`workout-feedback`，以及它们的 `references/`、`evals/` 和 `experience/docs/`，均从三途项目 `agent-package/` **逐文件原样复制**。没有改写 Skill 正文。每个文件的 SHA-256 记录在 [原文校验清单](../experience/trimate-provenance.json)；构建宿主包时会校验内容。

旧版 `athlete-session-review`、`athlete-training-load`、`athlete-body-assessment`、`athlete-fueling`、`athlete-progress-review` 已从当前产品 Skill 目录移除，对话路由改用三途原名。当前项目独有的连接引导、目标确认、主观 Check-in、赛事策略和成长决策 Skill 继续保留。

三途原文有其完整后台依赖。当前产品的 [宿主执行契约](../experience/host-contract.md)单独列出可调用工具和缺口，不修改三途方法。11 个三途 Skill 均有对应工具入口；周期总结和海报、确认后写入训练记忆、跑姿视频异步分析已接入。跑姿分析仍需本机安装可选视觉依赖与模型文件；视频须先在本机暂存，聊天渠道不能直接上传。当前多周工具只提供重点项目轮换预测和逐周重新确认的七天计划；缺少自动渐进处方、餐单日志、自动写入或步骤评估等工具时应明确说明。

`app/services/training_load.py` 的计量口径此前已选择性迁入当前确定性服务；它与这次 Skill 原文复制是两项不同工作。来源项目的个人数据、凭据和运行时数据库没有迁入产品仓。
