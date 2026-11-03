# Trimate Skill 执行契约

本文件说明当前产品宿主能提供的工具与执行边界。不要把 Skill 中提到的数据库表、OpenClaw 命令或后台任务当成当前产品已经具备的工具。缺少执行能力时，说明缺口，不模拟成功。

| Skill | 当前可用产品工具 | 当前执行边界 |
|---|---|---|
| `endurance-athlete-assessment` | `get_profile_summary`, `list_goals`, `get_sport_baseline` | 可渐进式询问与解读；无能力快照写入工具 |
| `body-assessment` | `get_today_report`, `get_recent_check_ins`, `get_training_load_context`, `get_sport_baseline` | 可基于已有证据评估；没有六周专项趋势和结构化评估保存时，不声称已有或已保存 |
| `athlete-daily-review` | `get_sync_status`, `get_today_report`, `get_weekly_report`, `list_activities`, `list_notifications` | 日报与周报含独立的夜间 HRV 数值对比；不凭 HRV 单独判定当日准备度或自动改课 |
| `injury-risk` | `get_today_report`, `assess_return_to_training` | 非诊断性解释；症状资料不足时询问，安全结果优先 |
| `training-load` | `get_training_load_context`, `get_recent_check_ins`, `get_today_report` | 设备负荷和明确 RPE 分口径；当前无 `training_loads` 写入、TSS/疲劳代理计算或自动风险等级 |
| `workout-feedback` | `get_activity_detail`, `analyze_session`, `get_recent_check_ins`, `get_today_report`, `list_training_plans`, `list_plan_execution`, `compare_plan_actual`, `preview_plan_execution`, `confirm_plan_execution` | 可解释活动、同距离分圈前后对比和相似历史参考；计划课与活动只生成候选，用户核对后才可确认执行状态、RPE 与疼痛。无自动完成判定、逐点样本分析、逐段评估写入或 `ai_feedback` 写入 |
| `weekly-review` | `get_weekly_report`, `get_training_load_context`, `get_recent_check_ins`, `list_training_plans` | 可做已有证据的周复盘；缺计划与实际逐项关联时不计算完成率 |
| `training-plan` | `get_today_report`, `get_training_load_context`, `get_sport_calibration`, `list_goals`, `list_training_plans`, `get_training_preferences`, `preview_plan`, `preview_plan_blocks`, `list_plan_execution`, `review_plan`, `preview_plan_adjustment`, `preview_plan_update`, `confirm_plan` | 支持私有偏好、七天草案、2–12 周重点轮换预测、同周单节课的保守改动预览与版本确认；新活动与课表关系须由用户核对。当前预览不超过最近七天已观察分钟数；七天无活动时限于短时低强度草案。未来周不自动加量；较轻周和赛前周减量仍为待复核启发式。完整逐日专项周期处方和结构化步骤尚未执行 |
| `training-nutrition-coach` | `get_today_report`, `preview_fueling_plan`, `preview_fueling_logistics` | 当前仅支持一般训练/比赛补给预览；无饮食日志、餐单、能量账本写入 |
| `periodic-training-summary` | `get_periodic_training_summary`, `render_periodic_training_poster` | 从当前用户私有库按日历周期汇总；可输出 PNG 海报，缺失计划关联时不计算完成率；不走 OpenClaw 投递 |
| `training-memory` | `preview_training_memory`, `confirm_training_memory`, `list_training_memory`, `get_training_preferences`, `preview_training_preferences`, `confirm_training_preferences` | 自由文本偏好笔记不自动转为排课参数；结构化偏好经预览/确认后写入私有画像。部分身体状态不等于完整 Check-in，也不自动解除安全限制 |
| `running-form-analysis` | `get_running_form_runtime`, `start_running_form_analysis`, `get_running_form_job`, `get_running_form_contact_sheet` | 本地 CLI 暂存视频后，MCP 仅接收当前用户的任务 ID；后台执行姿态、评分和报告逻辑，完成后删除暂存原视频。缺少可选依赖或姿态模型时返回 `setup_required`；未评估维度保持未评估，不做医学诊断。可在结果完成后读取带证据标注的关键帧拼图；当前不支持在聊天渠道直接上传视频。 |

跑姿视频的本机入口是 `athlete-growth running-form-stage <视频路径>`；模型与依赖状态由 `athlete-growth running-form-runtime` 检查。模型存于私有数据目录，不进入产品仓库。用户本机需运行 `athlete-growth running-form-setup` 在私有目录创建 Python 3.12 专用环境，并运行 `athlete-growth running-form-install-model` 下载 Google 官方 Pose Landmarker Full 模型；也可用 `--from-file` 安装兼容的 `.task` 模型。模型未就绪时不能声称跑姿已分析。

所有 Skill 的写入指令都受当前产品 Agent 的用户确认与工具边界约束：先展示具体记录或草案，仅在用户明确同意且存在对应产品工具时写入。Skill 中的数值阈值和状态标签不能覆盖当前确定性安全结果；没有可比数据时不推断成绩、伤病风险或“可以加量”。
