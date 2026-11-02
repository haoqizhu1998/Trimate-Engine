# 耐力运动产品的 Skill 与工具盘点

目标场景：马拉松、铁人三项、骑行和游泳训练，以及比赛表现的持续改进。本页盘点**当前代码已经提供什么**，并给出下一轮建设顺序；建议项不等于已交付。

## 职责边界

| 层 | 负责什么 | 不负责什么 |
|---|---|---|
| 常驻Trimate Agent | 每轮识别意图、组合场景、处理冲突、解释证据与不确定性 | 自行制造个人指标或绕过确认 |
| 场景 Skill | 对一类用户任务规定提问、取证、取舍与呈现方法 | 保存事实、计算指标或覆盖安全规则 |
| 产品工具 / MCP | 查询私有数据、预览确定性结果、执行经授权的写入 | 充当训练方法论或自动推断用户同意 |
| 领域规则 | 单位、来源、计算、适用范围、安全与权限约束 | 随一次对话反馈自行改写 |
| 外部连接器 | 同步设备、赛事或文件数据，标记来源、质量和版本 | 直接生成无验证的训练决策 |

## 当前实际交付

版本化清单见 [manifest](../experience/manifest.json)。产品登记 **17 个 Skill**，均由本产品维护并配有工具入口；各能力覆盖范围不同，详见 [宿主契约](../experience/host-contract.md)。

| Skill | 当前任务 | 主要边界 |
|---|---|---|
| `athlete-onboarding` | 新用户、连接、数据可用性 | 宿主和本机安装状态仍要逐一验收 |
| `athlete-daily-review` | 解释当日与近期报告 | 只回答“发生了什么”，不代替行动确认 |
| `endurance-athlete-assessment`、`body-assessment` | 基础画像与当前状态评估 | 能力快照写入和完整六周趋势尚缺工具 |
| `workout-feedback`、`training-load` | 单次训练反馈与负荷解释 | 当前仅执行已有记录的分析，不自动写入 |
| `injury-risk` | 疼痛与伤病风险信号 | 产品确定性安全结果优先 |
| `athlete-goal-plan` | 目标草案与确认；基础七天工具入口 | 长期周期化方法由 `training-plan` 承载，当前执行工具尚未覆盖 |
| `athlete-recovery` | 症状、复训边界、主观 Check-in | 非诊断，缺少 Check-in 视为未知 |
| `training-nutrition-coach` | 饮食、训练与比赛补给 | 当前只有补给预览工具，没有饮食日志或餐单生成 |
| `athlete-race-strategy` | 赛事信息、跑步执行策略及用户输入的分项目赛段换算 | 赛段工具不预测成绩或功率目标 |
| `training-plan`、`weekly-review` | 周期计划方法与周度复盘 | 当前计划工具仍仅支持基础七天草案 |
| `periodic-training-summary`、`running-form-analysis`、`training-memory` | 周期海报、视频分析、训练记忆 | 已接入执行入口；跑姿需本机视觉依赖、模型和本地视频暂存 |
| `athlete-decision-coach` | 今日选项、执行反馈、受限适应 | 当前选项偏粗，不能自动滚动改写计划 |

当前 MCP 目录有 **40 个工具**，其中 29 个标记为只读或预览，11 个涉及写入或主动同步。按产品用途分组：

| 类别 | 数量 | 现有工具 |
|---|---:|---|
| 入口与档案 | 3 | `get_capability_catalog`, `get_onboarding_status`, `get_profile_summary` |
| 数据与同步 | 4 | `get_sync_status`, `run_sync_now`, `list_activities`, `get_data_quality` |
| 报告 | 2 | `get_today_report`, `get_weekly_report` |
| 单次与专项表现 | 5 | `get_activity_detail`, `analyze_session`, `get_sport_baseline`, `get_training_load_context`, `compare_plan_actual` |
| 目标与计划 | 7 | `list_goals`, `list_open_planning_drafts`, `propose_goal`, `confirm_goal`, `preview_plan`, `confirm_plan`, `list_training_plans` |
| 主观状态与恢复 | 4 | `get_recent_check_ins`, `preview_check_in`, `confirm_check_in`, `assess_return_to_training` |
| 补给与赛事 | 4 | `preview_fueling_plan`, `preview_fueling_logistics`, `preview_race_strategy`, `preview_race_segments` |
| 反馈与成长决策 | 9 | `list_growth_history`, `analyze_response_samples`, `get_growth_profile`, `preview_training_decision`, `confirm_training_decision`, `list_training_decisions`, `record_decision_outcome`, `confirm_growth_adaptation`, `revoke_growth_adaptation` |
| 通知 | 2 | `list_notifications`, `mark_notification_read` |

这些工具是**产品用例接口**，不等于 40 项成熟的耐力运动分析能力。`list_activities` 返回简要信息；详细活动工具可读取 TCX/FIT 导入的最多 200 个分段，以及存在于记录中的平均功率和踏频，但 Garmin 中国区的摘要同步通常没有分段。工具不读取原始路线，也不凭缺失的字段推断阈值。[实现位置](../src/athlete_growth/interfaces/host/capabilities.py)

## 划分与实现上的主要问题

1. **补给与赛事原本混在一个 Skill。** 二者输入、证据和结果不同，现已拆成两个场景方法；同一比赛请求仍可组合使用。
2. **赛事单位必须按项目区分。** 跑步策略仍仅支持跑步；新增赛段工具分别计算游泳 min/100m、骑行 km/h、跑步 min/km，并计入铁三两次换项。数值来自用户输入，尚未经过能力、路线、天气和补给约束校验。
3. **“目标与计划”名称仍宽于实现。** 现有计划器支持七天排课、多周上限预测，以及赛前一周和比赛周的保守减量提示；不能声称生成经过个体化验证的马拉松周期计划、铁三专项训练量比例或完整赛前处方。[实现位置](../src/athlete_growth/application/plan/service.py)
4. **单次训练链路已有描述性闭环。** 可读活动与分段、同项目历史中位数，并将指定计划课与活动作时长/项目/日期对照。`workout-feedback` 的自动写入与逐步评估仍缺产品工具；未完成原因和技术分析也不能补猜。
5. **“今日复盘”和“今日决策”容易重叠。** 已在 Agent 路由中明确：前者解释证据，后者给可确认选项并记录结果；长期趋势再交给成长复盘。
6. **补给数值仍很粗。** 本轮修正了短比赛被套用长距离碳水目标、仅因气温升高就机械增加饮水量的规则；后续仍需把实际补给点、已训练的肠胃耐受证据、汗率测量、赛事补给点和专业营养边界引入。运动营养建议应与运动时长、场景和个体耐受相匹配；必要时由运动营养专业人员个体化制定。[ACSM 联合立场声明](https://pubmed.ncbi.nlm.nih.gov/26891166/)
7. **数据连接器、Skill、工具和规则不能混为一层。** Garmin/文件/API 是来源；`run_sync_now` 是运维用例；`preview_plan` 是产品工具；“如何共创目标”是 Skill；安全与单位转换属于确定性规则。

## 下一轮应补的能力

| 顺序 | 新增能力 | 更适合的承载方式 | 验收点 |
|---|---|---|---|
| 已交付基础 | 跑、骑、泳的正确单位与铁三总配速保护；TCX/FIT 分段摘要 | 单次分析服务、连接器、课后复盘 Skill | 不把总铁三公里数算成跑步配速；可解释缺字段与质量标记 |
| 已交付基础 | 单次训练课与计划对照 | `get_activity_detail`、`analyze_session`、`get_sport_baseline`、`compare_plan_actual` | 用户明确指定对应关系；不把设备记录当作完成原因或主观 RPE |
| 已交付基础 | 用户确认的专项测试参考与宽区间 | 跑步阈值配速、骑行 FTP、游泳 CSS 的版本化预览/确认工具 | 测试结果有日期、来源和版本；过期或估计值不作为计划区间；仍需真实测试协议校准 |
| 随后 | 马拉松与铁三完整多周逐日处方 | 当前已有滚动七天计划、重点轮换、恢复周与赛前/赛事周保守上限预测 | 赛事日期、训练史、跑骑泳比例、关键课和个体化减量期形成完整处方；未来周仍逐周确认 |
| 随后 | 分项目赛事策略 | 独立的赛道、天气、能力和补给约束模型；赛事策略 Skill 协调 | 从用户输入的赛段换算升级到可验证的策略；赛道和天气有来源 |
| 随后 | 补给练习与赛中执行 | 补给日志、耐受反馈、补给点与携带约束；补给 Skill 协调 | 训练中验证方案，不把尚未练过的数值当比赛处方 |
| 持续 | 反馈驱动的个体化评估 | 成长决策用例、证据规则与用户确认 | 区分相关与因果，追踪建议接受率、完成率、表现与副作用 |

多项目训练不能只用一个“总负荷”数字作决策。IOC 建议联合监测项目相关的外部负荷、内部反应和主观状态，且没有单一指标能稳定预测适应不良。[IOC 负荷共识](https://bjsm.bmj.com/content/50/17/1030.long) 铁三尤其需要处理骑行后的跑步反应，研究显示存在影响，但具体踏频或强度的通用最优策略并不明确。[骑跑转换综述](https://pubmed.ncbi.nlm.nih.gov/35871903/) 专项减量与赛前准备应由赛事需求、训练史和近期状态决定，而不是复用固定 7 天模板。[耐力减量综述](https://pubmed.ncbi.nlm.nih.gov/37163550/)

身体目标不能凌驾于表现与健康边界之上；长期能量不足可能同时损害健康和耐力表现。产品应识别值得专业评估的线索，而不是自行诊断。[IOC REDs 共识](https://bjsm.bmj.com/content/57/17/1073)

## 交付边界

已交付：17 个产品 Skill 与参考资料入仓并接入工具入口；分项目训练量和近期主观记录只读工具；四个描述性活动工具、一个用户输入赛段换算工具、TCX/FIT 分段保留、用户声明补给机会核算、基础 7 天跑骑泳轮换，以及周期总结海报、训练记忆写入与跑姿视频本机异步分析。跑姿视觉环境和模型仍需单独安装，尚未用真实视频做端到端验收。上述“随后”项目仍待开发；现有产品尚不能可靠生成铁三专项周期计划、骑行功率策略或自动游泳比赛分段目标，也不能保证比赛成绩提升。
