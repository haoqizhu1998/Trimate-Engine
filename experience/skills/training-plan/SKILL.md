---
name: training-plan
description: Use when 用户明确要求制定、预览、调整或恢复赛事导向的长期耐力训练计划，或已有执行中计划且用户询问下一周安排。支持公路跑、公路骑行和铁人三项。
metadata:
  version: "1.12.0"
  categories:
    - running
    - cycling
    - triathlon
    - endurance
    - planning
---

# 周期化训练计划

## 使用边界

仅在以下情况使用：

- 用户明确要求生成、更新、暂停、恢复或查看长期赛事训练计划。
- 用户已有执行中计划，询问“下周练什么”或“接下来怎么练”。
- 新增赛事、修改赛事目标或训练条件发生变化，需要重排已有计划。

用户只是在记录训练、询问一次训练反馈或泛泛问建议时，不要生成完整计划。没有已有执行中计划时，“下周怎么练”只给保守短建议，并确认用户是否要建立长期计划。

“下周练什么”或“接下来怎么练”只有在已有执行中计划时才按计划调整。

首版赛事范围：

- 跑步：5K、10K、半程马拉松、全程马拉松。
- 骑行：计时赛、长距离公路骑行。
- 铁三：Sprint、Olympic、70.3、Ironman。

越野、超马和独立游泳赛事不生成正式计划；说明当前范围后，可给原则性建议。铁三中的游泳训练正常支持。

## 总原则

- 用户目标优先，但必须独立给出安全判断，不能为了迎合目标隐藏风险。
- 大白话优先；TSS、CTL、ATL、TSB 等术语首次出现时顺手解释。
- TSS 是统一负荷核心，时长、距离、配速、功率和主观感受用于辅助理解。
- 计划是可调整版本，不是一次性承诺。每次新增赛事、目标、身体状态或日程变化后重新评估。
- 每条回复最多只问一个关键问题。非安全关键缺失字段使用明确的保守默认值。
- 不虚构数据、能力、历史或精确成功概率。
- 跑步、公路车、游泳和铁三的共用处方先遵守 `../../docs/endurance-training-methodology.md`，尤其是强度口径、跨项目关键课密度、负荷多指标解释和动态调整闭环。
- 跑步计划参考 `../../docs/running-coach-principles.md`。这些原则用于中长跑、公路跑和铁三跑步段，不硬套到骑行或游泳。

## 工作流

### 1. 判断是否触发

先确认这是长期计划请求、已有计划的近期安排，还是一次训练建议。只有前两类进入本 Skill。

### 2. 安全门

在计算计划前读取 `references/adjustment-and-safety.md`。

- 命中医学硬红线时停止生成继续训练方案，只提供停止训练和寻求专业评估的建议。
- 非医学高风险目标进入“双轨计划”：用户目标轨 + 安全建议轨。

### 3. 获取最小上下文

读取 `references/intake-and-data.md`，按分层策略只取必要数据。最低可生成预览的条件：

- 至少一个赛事项目与日期。
- 每周可训练时间或训练天数。

当前体能、近期训练和目标成绩缺失时可以估算，但必须标注假设、来源与置信度。一次最多只问一个会显著改变计划的问题。

### 4. 评估目标和负荷

读取 `references/load-and-zones.md`：

- 建立跑步 VDOT、骑行 FTP、铁三分项强度区间。
- 统一估算 TSS，并用大众指标解释。
- 计算或谨慎解释 CTL、ATL、TSB；数据不足时不伪造精确值。
- 判断目标与可用周数、训练基础、恢复能力是否匹配。
- 调整关键课前，通过 `step-trend` 读取 `workout_step_assessments` 与 `workout_step_results` 的历史完成证据；只有至少 3 次可比关键课才形成趋势信号。
- 表现趋势必须先通过疲劳、疼痛和恢复状态组成的恢复门。允许进阶时也只增加一个变量；趋势仅用于生成调整预览，不得静默修改 active 计划。

### 5. 生成周期结构

读取 `references/periodization.md`：

- 支持多主赛、多热身赛和多峰安排。
- 从当前状态过渡到基础期、进阶期、巅峰期、减量期和赛后恢复。
- 每 3–4 个负荷周安排恢复窗口，并按个体反馈调整。
- 默认加入每周 1–2 次力量训练，冲突时优先保护关键专项课和恢复。

### 6. 输出预览

严格使用 `references/output-template.md`：

- 输出从本周到最后一个纳入规划赛事的完整周纲要。
- 详细展开最近 4 周到每天；更远周次保留阶段、周量、关键课和强度分布。
- 每次训练使用稳定训练 ID，便于完成、跳过、移动和替换。
- 第一次生成或重大改版状态为 `preview`，明确等待用户确认后才成为 `active`。

### 7. 调整执行中计划

按照 `references/adjustment-and-safety.md`：

- 普通漏训只局部重排，禁止堆叠补课。
- 新赛事、目标变化、持续疲劳、疼痛、能力明显变化触发重大改版。
- `step-trend` 返回 `progression_candidate` 时，仍要先过恢复门，并且只增加一个变量；返回 `hold_for_recovery` 或 `reduce_or_simplify` 时优先保持、降量或简化关键课。
- 对 active 计划中的未来 `planned` 关键课调用 `plan-adjustment-preview`，只生成一条字段级差异。展示修改前、修改后与理由，并保留 `requires_confirmation = true`、`persisted = false`；不得把预览描述成已生效。
- 只有用户明确确认该预览后，才调用 `plan-adjustment-apply --confirm-preview-token <preview_token>`。应用前必须重新核对 `source_version`；过期预览应停止并重新生成。成功时原子更新唯一字段并写入 `training_plan_adjustment_audits`，相同令牌重试必须幂等。
- 用户要求撤销已应用的调整时，先调用 `plan-adjustment-undo-preview --adjustment-audit-id <audit_id>` 生成撤销预览，展示原调整和反向字段差异；“后悔了”“改回来”等表达只触发预览，不能推断为写入授权。
- 只有用户明确确认撤销预览后，才调用 `plan-adjustment-undo-apply --confirm-undo-token <undo_token>`。应用前必须重新核对 `source_version`；计划后来发生变化时拒绝旧令牌。有效撤销应原子恢复唯一字段并写入 `training_plan_adjustment_reversals`，相同令牌重试必须幂等。
- 对话中展示调整或撤销预览时，优先调用 `plan-adjustment-stage` 暂存当前用户的唯一待确认项，默认有效期为 30 分钟；新预览取消旧待确认项。展示差异后明确提示用户只需回复“确认”或“取消”，不得要求用户复述令牌。
- 用户明确回复“确认”时调用 `plan-adjustment-pending-confirm`；明确回复“取消”时调用 `plan-adjustment-pending-cancel`。不得把“好的”“知道了”等一般回应推断为确认。待确认项过期或源计划变更时不得写入，应重新生成预览。
- “确认”“确认调整”“确认撤销”“取消”“取消调整”“取消撤销”这些完整独立指令使用渠道层确定性路由并绕过模型；相同词语出现在普通语句中时不得命中该路由。
- 暂存、刷新、替代、确认、重复确认、取消、过期、失效和无待确认项写入当前用户的 `training_plan_approval_events`。需要排查确认漏斗或异常重试时使用 `plan-approval-metrics`；事件只保留结构化状态，不保存聊天正文、预览令牌或渠道身份明文。
- `plan-approval-metrics` 的确认率是所选时间窗内的窗口比率，不是同批审批的 cohort 转化率。健康检查在至少 5 个暂存事件或 5 次确认尝试的最小样本后，才分别评估 `high_expired_rate`、`high_stale_rate` 和 `high_retry_signal_rate`；小样本仅展示数据，不触发比率告警。任何逾期待确认项都立即告警，不受样本门槛限制。
- `plan-approval-metrics` 顶层指标继续表示窗口事件比率；`cohort` 区块以窗口内首次暂存的审批为集合，使用 `outcome_counts` 和 `resolution_rate` 展示确认、取消、过期、失效、被替代、待处理等最终去向。窗口外首次暂存、仅在窗口内完成的审批不得混入该 cohort；同一审批刷新后只按最新生命周期归因一次。
- cohort 的速度指标从最新生命周期开始计时：`confirmation_latency_minutes` 汇总确认耗时，`resolution_latency_minutes` 汇总所有终态耗时，均返回样本量、中位数和采用 nearest-rank 口径的 P90。`pending_age_minutes` 汇总未解决项年龄与 `lt_15m`、15–29、30–59、60 分钟以上分层；不得从少量样本推断稳定趋势。
- `trend` 按自然日 `daily` 和周一对齐的自然周 `weekly` 输出完整 cohort；`maturity_minutes = 60` 表示必须经过 60 分钟观察窗，窗口首尾的残缺周期不参与比较。`minimum_cohort_size = 5`，只有连续三个完整日都达标且出现两次同向恶化时，才返回 `declining_cohort_confirmation_rate`、`rising_cohort_expired_rate` 或 `rising_cohort_confirmation_p90`；小样本状态为 `insufficient_data`，不得告警。
- 每个趋势点的 `segments` 同时提供 `by_operation` 和 `by_initial_source`；后者严格表示 approval 的首次暂存入口，不随刷新入口改写。实现必须对窗口生命周期批量加载，以固定次数查询生成总 cohort、日/周 cohort 和分层指标，禁止按每个时间桶重复访问数据库。
- 重大改版先给变化摘要和新预览，确认后替换当前有效计划。
- `workout_step_assessments` 与 `workout_step_results` 是复盘证据，不是自动改计划指令；任何调整都不得静默修改执行中计划。
- 始终只读取最新有效计划，不把所有历史计划塞入上下文。

## 相关 Skill

- 基础能力与目标信息不足时，可结合 `endurance-athlete-assessment`，但仍遵守一次最多一个问题。
- 周度执行和身体趋势来自 `weekly-review`。
- 明显疼痛和伤病风险交给 `injury-risk`。
- 未来单独提供 `race-strategy-and-fueling`，负责比赛策略和补给；本 Skill 只输出赛事专项训练模块的简要提示。
