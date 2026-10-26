# 产品技术架构

## 1. 架构目标

产品需要同时满足八个目标：

1. **个人可用**：优先服务单个运动者，安装简单、数据本地可控。
2. **证据可追溯**：每个指标、分析和建议都能追溯到来源数据、算法版本和模型运行。
3. **连接可替换**：Garmin 中国区、国际区、文件导入或未来其他设备互不侵入核心业务。
4. **模型可替换**：OpenAI、Anthropic、Gemini、兼容 OpenAI API 的服务和本地模型通过统一接口接入。
5. **产品可发布**：源码、运行数据和内部研发过程彻底分离，可安全开源或打包商业版本。
6. **用户可控制**：用户能查看授权、限制 AI 外发、导出、备份、更正和删除自己的数据。
7. **闭环可学习**：系统同时接收客观数据、主观感受和计划执行反馈，逐步学习个人响应。
8. **宿主可迁移**：用户在 ChatGPT/Codex、WorkBuddy、Coze 等熟悉的模型宿主中使用产品，换宿主不迁移个人数据，也不改变业务结论。

## 2. 核心架构决策

- 采用**本地优先的模块化单体**，首阶段不引入微服务。
- Python 延续现有资产，核心业务代码保持与 Garmin、数据库、Web 框架和具体 AI SDK 无关。
- 使用端口与适配器隔离外部系统，但不在 MVP 阶段建立复杂的动态插件市场。
- 训练指标由确定性算法计算；AI 负责解释、归纳、交互和计划草拟，不充当事实计算器。
- 原始数据不可变；修正和派生数据以新版本保存，不覆盖来源证据。
- 所有写回 Garmin、删除数据或安排训练的动作必须经过显式确认。
- 日常分析必须结合可选的主观 Check-in，不能把缺失的主观状态推断为“状态良好”。
- 安全规则先于 AI 建议执行；疼痛、生病或异常风险信号可以限制计划，但不能被产品表述为医学诊断。
- 数据授权、AI 外发、导出、备份、更正和删除是基础能力，不延后到商业化阶段。
- 采用**宿主优先、核心宿主无关**的产品形态：统一 Tool Catalog 是用户入口，MCP 和 HTTP/OpenAPI 只是传输适配，CLI 仅作为开发、诊断、恢复与降级入口。
- 新手引导进度保存在应用层状态机中，不依赖模型记忆、Prompt 或单次对话历史。
- 自动同步、分析和通知通过持久化事件串联；外部信号允许重复，内部处理必须幂等、可恢复且可审计。

## 3. 系统关系

```mermaid
flowchart LR
    Sources[Garmin CN / Personal API / 文件 / 赛事来源] --> Signal[Push / Poll / Manual]
    Signal --> Sync[同步协调器]
    Sync --> Connectors[连接器适配器]
    Connectors --> Ingest[采集与标准化]
    CheckIn[主观 Check-in / 生活情境] --> Ingest
    Ingest --> Raw[(原始数据区)]
    Ingest --> Canonical[(标准化数据区)]
    Ingest --> Changes[(变化集与持久化任务箱)]
    Changes --> Analytics[确定性分析引擎]
    Canonical --> Analytics
    Profile[个人资料 / 目标 / 约束] --> Analytics
    Analytics --> Derived[(派生指标与证据)]
    Derived --> Safety[安全规则与风险升级]
    Safety --> Coach[计划与成长服务]
    Profile --> Coach
    Coach --> Reports[日报 / 周报 / 计划 / 赛事策略]
    Coach -. 可选解释 .-> AI[AI 编排与模型适配器]
    AI -. 增强表述 .-> Reports
    Reports --> Notify[通知决策与 Outbox]
    Notify --> Inbox[用户未读收件箱]
    Notify --> Delivery[系统 / 邮件 / 宿主通知适配器]
    Feedback[执行结果 / 用户纠正] --> Learning[个人响应学习]
    Reports --> Feedback
    Learning --> Analytics
    Reports --> Tools[统一 Tool Catalog]
    Inbox --> Tools
    Onboarding[应用层引导状态机] --> Tools
    Tools --> MCP[MCP 适配器]
    Tools --> HTTP[HTTP / OpenAPI 适配器]
    Tools --> CLI[维护与降级 CLI]
    MCP --> GPT[ChatGPT / Codex]
    MCP --> WB[WorkBuddy]
    HTTP --> GPT
    HTTP --> Coze[Coze Agent / Workflow]
    Tools -. 阶段 6 .-> Web[未来独立 Web / App]
```

## 4. 业务模块

### 4.1 Athlete Profile

- 基础资料、时区、单位制、运动偏好；
- 身高、体重、心率区间、阈值和健康限制；
- 可训练时间、不可训练日、场地、装备和训练环境；
- 数据授权和隐私偏好。

### 4.2 Observations

- 运动活动：跑步、骑行、游泳、力量训练和多运动；
- 身体数据：睡眠、HRV、静息心率、压力、身体电量、血氧、体重和体成分；
- 主观 Check-in：疲劳、酸痛、疼痛、情绪、训练意愿、疾病与 RPE；
- 生活情境：工作压力、旅行、时差、酒精、环境和用户选择记录的其他因素；
- 数据质量、来源、采集时间和原始记录引用。

主观记录默认由用户明确填写；系统不得把未填写解释为“无疼痛”或“恢复良好”。

### 4.3 Goals

- 运动目标：频率、里程、时长、能力或技术；
- 身体目标：体重、体成分、睡眠、恢复或健康习惯；
- 赛事目标：完赛、目标成绩、配速、功率或名次；
- 目标具有基线、目标值、截止日期、优先级、状态和检查周期；
- 支持识别目标冲突、硬约束和不同方案的取舍，不静默牺牲用户的高优先级目标。

### 4.4 Analytics

- 训练量、强度分布、连续性、单调度和负荷突增；
- 心率漂移、配速/功率表现、分段和个人最佳；
- HRV、睡眠、静息心率和身体电量的个人基线与异常；
- Fitness/Fatigue/Form、恢复状态和计划完成度；
- 数据源冲突、缺失、设备变化和异常值检测；
- 所有结论返回指标值、置信度、数据缺口、算法版本与证据引用。

### 4.5 Planning & Coaching

- 根据目标、可用时间、能力和恢复情况制定训练计划；
- 计划状态：草拟、已批准、已安排、已完成、部分完成、跳过、替代、取消；
- 计划与实际对比、未完成原因、自动调节建议和周期复盘；
- 身体调理与恢复建议；
- 饮食和补给计划；
- 赛事配速、功率、补给、装备和风险策略。

计划调整必须记录原因、依据和影响；突破用户硬约束的建议必须被拒绝，而不是仅提示。

产品只能提供训练与生活方式建议，不能替代医疗诊断。出现持续异常、疼痛或高风险信号时应建议用户寻求专业医疗意见。

### 4.6 Safety & Data Rights

- 安全信号：疼痛、疾病、异常恢复趋势、过快负荷增长和用户声明的禁忌；
- 风险动作：降低强度、暂停自动调整、建议休息或建议寻求专业帮助；
- 授权中心：数据源权限、AI 外发范围、保留期限和撤回授权；
- 用户权利：查看、更正、导出、备份、恢复和删除；
- 所有安全决策和授权变更进入审计记录。

### 4.7 Growth & Personal Learning

- 成长时间线、个人最佳、里程碑、连续性和阶段复盘；
- 记录用户是否采纳建议、执行结果和主观效果；
- 比较不同训练刺激后的个人恢复和表现响应；
- 支持受控的个人实验，但区分相关性与因果结论；
- 知识来源、分析方法和规则版本可追溯。

### 4.8 Ecosystem Connectors

- Garmin 中国区本地会话只读连接器，使用独立 Token 目录；
- Garmin 国际区 Personal API 连接器；
- MCP 可作为模型宿主的本地或远程工具传输，但不是 Garmin 数据采集运行时，也不持有 Garmin 凭据；
- Garmin FIT/TCX/CSV 和账户导出导入器；
- 赛事目录、路线、天气和成绩来源；
- 后续可增加 Apple Health、COROS、Suunto、Strava 等连接器。

### 4.9 AI Orchestration

- 统一的 `ModelProvider` 端口；
- 能力声明：文本、结构化输出、工具调用、长上下文、视觉；
- 路由策略：隐私、成本、速度、质量和是否允许出网；
- Prompt 版本、结构化响应 Schema、重试和降级；
- 模型运行日志只记录必要元数据，默认不保存敏感原文；
- 同一份确定性分析结果可以被不同模型解释，不重复计算核心指标；
- AI 输出携带证据覆盖率、风险级别和不确定性说明；
- 用户可以纠正 AI，纠正记录进入反馈闭环而不是覆盖历史输出；
- 云端模型调用前执行数据最小化和授权检查，本地模型可作为隐私优先选项；
- 固定评测集持续检查事实一致性、安全边界和模型降级。

### 4.10 Host Integration & Onboarding

- “模型宿主”和“模型 Provider”是两条独立轴线：前者承载用户交互，后者执行经授权的 AI 解释；用户在 ChatGPT 中操作不代表训练数据必须交给 OpenAI 模型；
- 版本化 `Tool Catalog` 统一暴露同步、分析、目标、计划、反馈和数据权利能力；
- MCP 适配 ChatGPT/Codex、WorkBuddy 等支持该协议的宿主；HTTP/OpenAPI 适配 GPT Actions、Coze Plugin 和其他宿主；
- `hosts/chatgpt`、`hosts/workbuddy`、`hosts/coze` 只保存安装清单、工具描述、对话指南、工作流和渲染模板，不复制领域规则；
- 引导状态由应用层持久化，覆盖建档、数据源选择、同步、基线、目标、Check-in、首周计划和激活；
- 引擎根据宿主能力协商传输、认证、展示和流式进度，不要求每个宿主实现完全相同的 UI；
- 本地模式使用一次性配对或本机权限；远程模式使用 OAuth/OIDC，并将外部身份映射到 `athlete_id`；
- 删除、导出、计划写回等敏感动作必须由引擎签发和校验短时效确认令牌，不能只依赖宿主文本确认。

### 4.11 Automation & Notifications

- 连接器声明 `push`、`poll` 或 `manual` 交付模式；官方推送和后台轮询最终进入同一同步协调器；
- Garmin Personal API 和当前 Garmin 中国区连接器采用增量轮询，设备唤醒或网络恢复后自动补拉；
- 同步批次仅在真正新增、替换或失效标准记录时生成 `DataChangeSet`，纯重复批次不触发重复分析；
- 分析作业以数据水位和算法版本作为幂等键，先执行确定性指标、数据质量和安全规则，再选择是否调用 AI 解释；
- 每次有效变化都生成可审计的 `AnalysisSnapshot` 和通知项；立即推送、摘要或仅保留未读由用户策略决定；
- 模型宿主不能被当作可靠消息队列：所有通知先写入本地 Outbox/Inbox，再由宿主、本机系统或邮件适配器投递；
- 后台运行器状态、上次/下次同步、任务积压、最近分析和投递失败通过统一工具查询；
- 本地模式依赖用户设备开机和后台运行器，自托管或托管模式才能提供全天候处理。

## 5. 已实现源码结构

产品仓使用包名 `athlete_growth`。阶段 0–5 的实际结构如下；阶段 5.5 的宿主适配、独立 Web/App 和发布系统尚未创建：

```text
athlete-growth/
├── pyproject.toml
├── README.md
├── src/athlete_growth/
│   ├── domain/
│   │   ├── athlete/
│   │   ├── goals/
│   │   ├── training/
│   │   ├── analytics/
│   │   ├── recovery/
│   │   ├── safety/
│   │   ├── growth/
│   │   ├── nutrition/
│   │   └── race/
│   ├── application/
│   │   ├── ingest/
│   │   ├── check_in/
│   │   ├── analyze/
│   │   ├── plan/
│   │   ├── learn/
│   │   ├── data_rights.py
│   │   ├── nutrition/
│   │   ├── recovery/
│   │   ├── race/
│   │   ├── ai/
│   │   └── report/
│   ├── ports/
│   │   ├── data_source.py
│   │   ├── repositories.py
│   │   ├── model_provider.py
│   │   └── workout_sink.py
│   ├── adapters/
│   │   ├── garmin_cn/
│   │   ├── files/
│   │   ├── races/
│   │   ├── storage/
│   │   └── ai/
│   └── interfaces/cli/
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── integration/
│   └── fixtures/
└── docs/
    └── adr/
```

阶段 5.5 计划新增以下边界；它们是目标结构，不代表当前已经实现：

```text
src/athlete_growth/
├── application/onboarding/   # 可恢复的引导状态机
├── application/automation/   # 同步协调、变化集、分析作业与恢复
├── application/notification/ # 通知策略、Inbox 与 Outbox
├── interfaces/tools/         # 统一 Tool Catalog 与结果契约
├── interfaces/mcp/           # MCP 传输适配
├── interfaces/http/          # HTTP/OpenAPI 与 webhook 传输适配
└── interfaces/worker/        # 本地/自托管后台运行器
src/athlete_growth/ports/
├── scheduler.py              # 时间与来源信号端口
└── notification.py           # 宿主、系统、邮件通知端口
src/athlete_growth/adapters/notifications/
└── ...                       # 各投递渠道的薄适配器
hosts/
├── chatgpt/                  # MCP/Plugin 与 GPT Action 描述
├── workbuddy/                # MCP 插件、Skill 与 Workflow
└── coze/                     # Plugin/OpenAPI 与 Workflow
```

### 依赖方向

```text
interfaces → application → domain
adapters   → ports       ← application
host packs → MCP/HTTP adapters → tool facade → application
domain 不依赖 Garmin、AI SDK、数据库或 Web 框架
```

连接器只产生标准化输入，不直接写数据库；应用层负责去重、校验、事务和同步游标。AI 只能通过应用层提供的工具读取经授权的数据。安全、授权和数据权利属于跨用例策略，但保持明确端口，不能散落成界面判断。

## 6. 数据架构

### 数据分区

1. **Raw**：来源返回的 JSON、FIT、TCX 或 CSV，按内容哈希去重且不可修改。
2. **Canonical**：统一单位、时区和字段后的活动、身体观测和目标。
3. **Derived**：基线、趋势、负荷、异常、计划完成度等可重算指标。
4. **Guidance**：训练计划、建议、报告、赛事策略和用户反馈。

### 关键实体

- `AthleteProfile`
- `AthleteConstraint`、`ConsentGrant`、`DataRetentionPolicy`
- `DataSourceAccount`、`SyncCursor`、`SourceRecord`
- `SyncSubscription`、`SyncRun`、`DataChangeSet`
- `Activity`、`ActivitySample`、`Lap`、`ExerciseSet`
- `BodyObservation`、`SubjectiveCheckIn`、`ContextEvent`、`DailyRecovery`
- `Goal`、`RaceEvent`
- `TrainingPlan`、`PlannedSession`、`CompletedSession`、`PlanDecision`
- `NutritionPlan`、`RaceStrategy`
- `MetricResult`、`Recommendation`、`EvidenceRef`、`SafetySignal`
- `AnalysisRun`、`AnalysisSnapshot`、`NotificationPolicy`、`NotificationOutbox`、`NotificationDelivery`
- `ModelRun`、`PromptVersion`、`UserFeedback`、`OutcomeFeedback`
- `PersonalResponseModel`、`GrowthMilestone`、`KnowledgeVersion`

每个核心实体从第一版开始携带 `athlete_id`；来源数据还需保存 `source_id`、来源记录引用、采集时间和 Schema 版本。未来多租户能力增加账户/租户归属，不改变个人训练领域模型。

### 存储策略

- MVP 使用 SQLite 作为标准化业务库，原始 FIT/JSON 保存在外部数据目录；
- Repository 端口隔离具体数据库，为未来 PostgreSQL 留出迁移能力；
- 大体量逐秒样本可以后续使用 Parquet，分析时由 DuckDB/Polars 读取；
- 数据库和原始文件均不进入产品 Git 仓库。

## 7. 运行目录和配置

源码仓之外使用独立数据目录，例如：

```text
~/.athlete-growth/
├── config.toml
├── secrets/
├── tokens/
├── profiles/
│   └── <athlete-id>/
│       ├── raw/
│       ├── db/
│       ├── derived/
│       ├── reports/
│       └── backups/
├── cache/
├── exports/
└── logs/
```

配置优先级：命令行参数 → 环境变量 → 用户配置文件 → 产品默认值。密钥和 Token 不允许写入普通配置、日志、错误报告或测试 Fixture。Garmin 密码只用于当次交互式登录，不持久化；中国区会话 Token 保存在 `tokens/garmin-cn-session/`，与国际区分离。

## 8. 接口契约

### DataSourceConnector

- `authenticate()`：建立或刷新授权；
- `capabilities()`：声明可读数据、可写动作、时间范围、交付模式和速率限制；
- `sync(since, cursor)`：增量获取并返回来源记录；
- `healthcheck()`：检查授权和服务状态；
- `revoke()`：清除本地授权。

### ModelProvider

- `capabilities()`：模型能力、上下文限制和隐私属性；
- `generate(request)`：结构化生成；
- `stream(request)`：可选流式响应；
- `estimate_cost(request)`：可选成本估算；
- `healthcheck()`：验证配置。

### DataRightsService

- `list_consents()`、`grant()`、`revoke()`：管理数据源和 AI 使用授权；
- `export_profile()`：生成包含 Schema 版本的可移植包；
- `backup()`、`restore()`：验证后备份和恢复；
- `correct_record()`：追加更正而不篡改原始记录；
- `delete_profile()`：预览影响、二次确认并生成删除证明。

### HostToolFacade

- `catalog()`：返回工具版本、参数 Schema、风险等级与宿主所需能力；
- `invoke(tool, input, context)`：验证身份、授权、幂等键和确认令牌后调用应用用例；
- `onboarding_state()`：返回当前引导状态、完成项、缺口和唯一推荐下一步；
- `capabilities()`：协商流式进度、OAuth、卡片展示和本地文件访问等宿主能力；
- 所有面向用户的结果统一包含 `summary`、`facts`、`evidence_ids`、`confidence`、`data_gaps`、`risk`、`next_actions`、`requires_confirmation` 和 `disclosure`。

### AutomationCoordinator

- `request_sync(source, reason, window)`：幂等登记推送、轮询或手动同步请求；
- `run_due(now)`：领取到期任务并记录心跳、重试和错误；
- `publish_changes(sync_run)`：只为真实数据变化创建变化集；
- `analyze(change_set)`：按数据水位和算法版本生成分析快照；
- `recover()`：重启后恢复未完成同步、分析和通知任务。

### NotificationPort

- `capabilities()`：声明是否支持主动推送、回执、卡片和敏感内容限制；
- `deliver(notification)`：幂等投递最小化通知内容；
- `status(delivery_id)`：查询回执或失败原因；
- 不支持主动推送的宿主通过 `notification.list_unread` 读取持久化 Inbox。

## 9. 安全与产品化边界

- 默认本地监听，远程访问必须鉴权；
- 敏感字段静态加密，日志脱敏；
- 外发给 AI 的字段按任务最小化，并展示数据去向；
- 健康、轨迹和身体数据按敏感个人信息处理，授权必须可查看和撤回；
- 用户可以从产品第一版开始导出、备份、恢复、更正和删除数据；
- 删除、训练写入、赛事报名等动作必须二次确认；
- 每个连接器有契约测试和速率限制；
- 自动同步、自动分析和主动通知均可由用户分别暂停，安静时段与通知粒度可调整；
- webhook 必须验证来源签名、防重放并与正确的 `athlete_id` 绑定；
- 商业版本不得依赖私有研发仓才能构建或运行；
- 开源样例只能使用合成或彻底匿名化数据。

## 10. 验证策略

- 单元测试：领域规则、指标计算、目标状态转换；
- 安全测试：疼痛、疾病、恢复异常和目标冲突不能被 AI 绕过；
- 契约测试：所有 Garmin/赛事/AI 适配器必须满足统一端口；
- 集成测试：使用脱敏 Fixture 验证同步、去重和数据库迁移；
- 自动化测试：重复信号、进程重启、网络恢复和部分失败不会丢任务或重复通知；
- 数据权利测试：导出可恢复、撤回授权立即生效、删除不遗留孤儿数据；
- Golden tests：固定输入对应固定训练指标和报告结构；
- AI 评测：事实一致性、证据引用、结构化输出、用户纠正、风险建议和模型降级；
- 回归测试：现有 783 条历史活动提取和 127 条 FIT→TCX 转换流程不得破坏。
