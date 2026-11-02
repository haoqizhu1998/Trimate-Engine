# 负荷、能力与强度区间

## 指标优先级

专业层以 TSS 为核心，大众层同步显示时长、距离和训练感受：

- TSS：训练压力分数，把不同时长和强度折算成统一负荷。
- CTL：较长期的训练负荷趋势，可理解为近期积累的“体能底子”。
- ATL：短期训练负荷趋势，可理解为最近累不累。
- TSB：CTL 与 ATL 的差值趋势，可理解为当前新鲜度，不等于医学恢复结论。

CTL、ATL、TSB 只有在连续数据足够时才给数值。数据不足时用“上升、平稳、偏疲劳”等区间性语言，不制造精确小数。

生成强度区间前先读取当前用户对应项目的能力快照，并核对测试日期、来源、置信度与有效期。过期能力快照不得继续生成看似精确的配速、功率或心率区间；改用 RPE、谈话测试或安排低风险校准测试。

生成后的数值区间写入 `training_zone_sets`，并保留 `zone_set_id`、能力快照 ID、`algorithm_key`、`algorithm_version`、生成时间、有效期和置信度。算法升级或能力快照变化时生成新版本，不能静默覆盖历史依据。计划中的数值处方必须引用对应 `zone_set_id` 与区间 key。

单段计划训练落库时，通过 `assign_planned_workout_zone` 把课程绑定到当前有效的 `zone_set_id` 与区间 key。包含热身、主训练、恢复、重复组和放松的结构化课程，使用 `replace_planned_workout_steps` 原子写入 `planned_workout_steps`；每一步分别保存 `zone_set_id` 与区间 key，重复组在结束步骤保存 `repeat_from_step_order` 与 `repeat_count`。休息步骤可以不设数值目标；没有有效能力锚点时，各步骤区间引用保持为空，改用 RPE 与谈话测试，不能伪造精确目标。

教练生成关键课时先用 `compile_workout_recipe` 编译处方，不要手工计算重复后的总时长。处方按 `warmup`、`main`、`cooldown` 分段；步骤使用 `duration_minutes`、`duration_seconds`、`distance_meters` 或 `open` 四选一，重复块使用 `repeat` + `steps`。每一步同时给 `intensity` 或更精确的 `rpe`，确保数值区间不可用时仍有可执行体感目标。通过本地入口落库时调用 `plan-compile --planned-workout-id ... --recipe-json ...`；编译器会展开重复块、补齐 RPE、更新可精确推导的总时长/距离，再原子替换结构化步骤。

能力快照更新、区间有效期变化或课程改期后，通过 `audit_plan_zone_references` 检查 active 计划：无结构课程审计整节课，有结构课程逐步骤审计，标出能力已替代、算法过期、课程日期超出有效期或引用不完整。审计只报告影响，重大调整仍先生成预览并征得确认，不得静默改写用户的执行中计划。

当前区间引擎按项目选择锚点：

- 骑行优先有效 FTP，使用 Friel/Coggan 阈值功率六区；缺少 FTP 时才考虑骑行专项阈值心率。
- 跑步优先有效阈值配速，缺失时使用跑步专项阈值心率。当前仅有 VDOT 时不强行换算精确配速，先使用 RPE 与谈话测试，等待可靠比赛或阈值校准。
- 游泳使用 CSS 生成轻松、稳定和阈值配速目标；高强度必须结合重复距离、休息和动作质量单独定标，不生成统一冲刺配速。
- 跑、骑、游的阈值心率和区间不能跨项目复制。

能力缺失、过期、没有明确 `valid_until`，或暂不支持数值换算时，返回三域 `RPE 与谈话测试`：轻松 RPE 2–3、中等至阈值 RPE 4–7、高强度 RPE 8–9；此回退不写成精确配速、功率或心率区间，也不持久化为精确区间版本。

## TSS 估算

按数据质量选择：

1. 有功率和 FTP：优先功率 TSS。
2. 有可靠阈值心率和连续心率：可用心率负荷近似。
3. 跑步有阈值配速或 VDOT：使用配速/强度与时长估算。
4. 只有时长和 RPE：使用独立 session-RPE 单位，不映射或冒充 TSS。
5. 数据更少：给保守范围，不给单点值。

每个 TSS 估算注明：

- 数据来源。
- 估算方法。
- 置信度：高、中、低。
- 会导致误差的主要因素。

## 跑步区间

优先使用近期有效比赛成绩推算 VDOT；其次使用阈值配速或可靠测试；再其次使用 RPE 和谈话测试。

输出至少包括：

- 轻松/恢复：能完整说话，RPE 2–3。
- 稳态/马拉松专项：呼吸受控，RPE 4–5。
- 阈值：只能说短句，RPE 6–7。
- 高强度/间歇：难以说话，RPE 8–9。

配速区间要给范围而非单点，并说明天气、坡度、疲劳会使配速失真。

## 骑行区间

优先使用近 6–8 周有效 FTP；其次用 20 分钟测试、近期功率曲线或 RPE 推断。输出功率范围、RPE 和用途。

FTP 过旧、测试环境变化或数据明显异常时降低置信度。没有功率计时，用心率、RPE 和谈话测试，不虚构瓦数。

## 铁三统一负荷

游、骑、跑分别估算负荷后合并 TSS，同时保留分项时长和关键专项负荷。不能因为总 TSS 合理就忽略跑步冲击、长骑后砖块跑或游泳技术疲劳。

## 强度分布

面向用户统一显示三类：

- 轻松：恢复和低强度有氧。
- 阈值：节奏、甜点区和阈值附近训练。
- 高强度：VO2max、短间歇和冲刺。

默认以轻松训练为绝大多数。比例随项目、阶段、能力和可训练时间变化，不机械套用固定 80/20。

## 逐段完成趋势

关键课调整前，通过 `step-trend` 查询当前用户在 `workout_step_assessments` 与 `workout_step_results` 中保存的逐段完成历史。趋势只比较项目、步骤类型和数据质量相容的课程；自动圈顺序匹配最高只有中等置信度。

- 少于至少 3 次可比关键课时返回历史不足，不据此加量或加难度。
- `progression_candidate` 表示近期可比步骤大多按目标完成，但必须再通过疲劳、疼痛和恢复状态组成的恢复门。
- `hold_for_recovery` 表示表现信号即使良好，也被近期疲劳或疼痛覆盖，应先恢复。
- `reduce_or_simplify` 表示近期关键课完成质量反复偏低，应降量、延长恢复或简化结构，不能安排补课堆叠。
- 允许进阶时只增加一个变量，例如只加一次重复、只延长单段时间或只缩短恢复，不能同时提高总量与强度。

逐段趋势是调整证据，不是计划写入命令。所有变化先进入预览并说明依据，获得确认后再激活；不得静默修改 active 计划。

对未来且状态为 `planned` 的目标课，可调用 `plan-adjustment-preview` 生成一条字段级差异：必须展示修改前、修改后、单位和理由，并原样保留 `requires_confirmation = true`、`persisted = false`。`progression_candidate` 只小幅增加一个时间或距离变量，自动取整后增幅不得超过 10%；`reduce_or_simplify` 优先只减少一次目标重复，无法安全减少重复时才缩短单段，缩短幅度不得超过 15%。多重复块课程只调整真正包含目标工作段的重复块。恢复门未通过、维持信号或历史不足时返回零改动。不得把预览描述成已生效。

预览返回 `preview_token` 与 `source_version`。只有用户明确确认预览中的字段级差异后，才调用 `plan-adjustment-apply --confirm-preview-token <preview_token>`；不能把普通对话中的肯定语气推断为确认。应用时重新生成预览并核对版本，计划或趋势变化导致令牌过期时停止写入并要求重新预览。有效确认在一个事务中原子更新唯一字段并写入 `training_plan_adjustment_audits`；同一用户、同一令牌的重试必须幂等，不得再次加量或重复记审计。

用户要求撤销已应用的调整时，先调用 `plan-adjustment-undo-preview --adjustment-audit-id <audit_id>` 生成只读撤销预览，并展示原调整及反向的修改前、修改后；“后悔了”“改回来”等模糊表达只授权撤销预览，不授权写入。预览返回 `undo_token`、`source_version`、`requires_confirmation = true` 和 `persisted = false`。只有用户明确确认撤销预览后，才调用 `plan-adjustment-undo-apply --confirm-undo-token <undo_token>`。应用前重新核对源版本；计划后来发生变化时拒绝旧令牌并要求重新预览。有效撤销应原子恢复唯一字段并写入 `training_plan_adjustment_reversals`；同一用户、同一令牌重试必须幂等。

在对话渠道展示调整或撤销预览时，优先使用 `plan-adjustment-stage`：传入 `planned_workout_id` 暂存调整，或传入 `adjustment_audit_id` 暂存撤销。它只保存审批状态，不修改计划；每个用户同时只有一个唯一待确认项，默认有效期 30 分钟，新预览取消旧待确认项。展示差异后告诉用户只需回复“确认”或“取消”，不得要求用户复述令牌。只有明确的“确认”才调用 `plan-adjustment-pending-confirm`，明确的“取消”才调用 `plan-adjustment-pending-cancel`；“好的”“知道了”等普通回应不构成确认。待确认项过期或 `source_version` 失效时不得写入，返回过期或失效状态并重新预览；重复确认只返回上一次结果，不再次修改计划。

“确认”“确认调整”“确认撤销”“取消”“取消调整”“取消撤销”这些完整独立指令使用渠道层确定性路由并绕过模型，直接按当前用户身份消费唯一待确认项。相同词语只要出现在普通语句中，例如“我确认今天跑了 8 公里”或“取消明天的训练”，就不得命中该路由，仍按普通对话处理。

每次审批状态变化写入当前用户库的 `training_plan_approval_events`，区分 `channel`、`cli` 和 `service` 入口。需要检查确认率、取消/过期/失效分布、重复确认或无待确认项等重试信号时，调用 `plan-approval-metrics`；统计必须按当前用户隔离，事件和结构化日志均不保存聊天正文、预览令牌或渠道身份明文。

审批健康检查使用固定的最小样本和保守阈值：至少 5 个 `staged` 才判断过期率与失效率，至少 5 次确认尝试才判断重试信号率。过期率高于 30% 返回 `high_expired_rate`，失效率高于 15% 返回 `high_stale_rate`，重复确认与无待确认项合计占确认尝试高于 25% 返回 `high_retry_signal_rate`。样本不足时状态为 `insufficient_data`，只展示窗口数据，不把偶发事件误报为比例异常；存在状态仍为 pending、但 `expires_at` 已早于统计截止时间的逾期待确认项时立即返回 `overdue_pending_approval`，不受最小样本限制。这里的 `confirmation_rate` 是统计时间窗中 confirmed/staged 的窗口比率，不是 cohort 转化率，不应用来推断每个暂存项的最终去向。

`plan-approval-metrics` 同时返回 cohort 漏斗，并明确区分两种口径：顶层计数和比率是窗口事件比率；`cohort` 仅选择统计窗内首次暂存的审批，然后观察到统计截止时间为止的最终去向。`outcome_counts` 与对应比率覆盖 confirmed、cancelled、expired、stale、superseded、pending、overdue 和 unknown，`resolution_rate` 只把前五种终态计为已解决。窗口外首次暂存、窗口内才确认的审批只影响窗口事件指标，不进入该 cohort。审批在取消或被替代后再次 refreshed 时，旧终态被新生命周期重置，每个 approval 只归因一次；不得输出审批 ID、令牌或聊天内容。

耗时必须从每个 approval 的最新生命周期开始计算，不能把 refreshed 之前的等待时间叠加进来。`confirmation_latency_minutes` 只统计 confirmed，`resolution_latency_minutes` 统计 confirmed、cancelled、expired、stale 和 superseded；两者都输出 `sample_size`、中位数 `median` 与 P90。P90 使用 nearest-rank 定义：对升序样本取 `ceil(0.9 × n)` 位置，避免不同实现产生不同结果。没有有效样本时中位数与 P90 返回 null，不返回伪造的 0。

`pending_age_minutes` 以统计截止时间减去最新生命周期开始时间，覆盖 pending 和 overdue，输出样本量、最老等待分钟数及 `lt_15m`、`15_to_29m`、`30_to_59m`、`gte_60m` 四档。单用户同时最多一个真实 pending，因此年龄分层主要用于当前审批的操作诊断；不能把单用户小样本包装成群体趋势。

`trend` 使用 `maturity_minutes = 60`：只有已经过 60 分钟观察窗且周期完整的 cohort 才进入趋势。`daily` 按自然日，`weekly` 按周一至下周一对齐；查询窗口开头和结尾的残缺日/周必须丢弃，不能用不成熟数据制造下降。每个点返回 cohort 大小、确认率、过期率、失效率、确认 P90 与样本充分性。

趋势比较的 `minimum_cohort_size = 5`。只有最近连续三个完整日均至少有 5 个审批，并出现两次同向恶化，才触发告警：确认率每步至少下降 15 个百分点为 `declining_cohort_confirmation_rate`，过期率每步至少上升 15 个百分点为 `rising_cohort_expired_rate`；`rising_cohort_confirmation_p90` 还要求三个日 cohort 各至少 5 个 confirmed，且 P90 每步至少增加 10 分钟。其他情况返回 `healthy` 或 `insufficient_data`，不把一次波动写成趋势。趋势 warning 只记录内部用户 ID、状态、时间窗和告警码，不保存渠道身份、令牌或聊天正文。

每个日/周趋势点还返回 `segments.by_operation` 与 `segments.by_initial_source`，分别拆分调整/撤销和 `channel`、`cli`、`service` 的首次暂存入口。`by_initial_source` 不随 refreshed 的执行入口改变，避免把生命周期刷新误记为获客入口。统计实现先批量加载整个窗口内的审批生命周期，再以固定次数查询生成总览、日/周和分层结果；不得为每个时间桶分别查询数据库。

## 目标可行性

综合可用周数、近期训练量、能力数据、恢复稳定性、伤病信号、赛事密度和生活限制，输出：

- 可行性等级：合理、偏激进、高风险、当前不可安全执行。
- 主要依据。
- 最大不确定项。
- 保守调整方案。

不得输出看似科学的精确概率或百分比概率，例如“成功率 73%”。可以给定性等级和条件式判断。
