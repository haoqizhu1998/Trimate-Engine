# ADR 0007：宿主优先的对话式产品交付

- 状态：已接受
- 日期：2026-10-26

> **实现状态（2026-10-26）**：本文描述的是目标架构，不是当前已安装的宿主能力。GPT/Codex、WorkBuddy、Coze 的 MCP/Plugin/OpenAPI 传输、版本化 Tool Catalog、持久化新手引导状态机、OAuth/OIDC 身份映射、自托管远程模式和托管模式均尚未实现。当前可用界面是本地 CLI、JSON-safe 的宿主无关 Python facade，以及 macOS `launchd` 调度器和系统通知适配器。

## 背景

本产品的主要使用方式不是让用户学习一组 CLI 命令，而是在 ChatGPT/Codex、WorkBuddy、Coze 等模型宿主中通过自然语言完成安装、授权、数据同步、分析、计划和反馈。各宿主的具体接入面不同，且会随平台变更：

- ChatGPT 可通过自定义 MCP/Plugin 或 GPT Actions 调用外部工具；远程 MCP 需要 HTTPS 端点或安全隧道，且可用能力受账户和工作区权限影响。
- WorkBuddy 通过插件系统支持 MCP 外部能力。
- Coze 可在 Agent 或 Workflow 中调用自定义插件，各工具以结构化输入输出参与工作流。

如果把业务规则、提示词、用户状态和数据访问逻辑分别复制到每个宿主，产品会出现结论不一致、权限失控和无法迁移的问题。

## 决策

### 1. 产品核心不属于任何模型宿主

`domain` 和 `application` 仍是唯一权威业务引擎。训练指标、安全规则、授权、去重、计划版本和用户数据均不存在于 ChatGPT、WorkBuddy 或 Coze 的 Prompt 中。宿主只负责对话编排、工具选择和结果展示。

### 2. 统一工具协议是主产品入口

产品对外暴露一份带版本的 Tool Catalog，再通过两种传输适配：

- MCP：作为支持 MCP 宿主的首选接入；
- HTTP/OpenAPI：作为 GPT Actions、Coze 自定义插件以及不支持 MCP 宿主的桥接。

CLI 继续保留，但定位改为开发、诊断、恢复和无宿主降级入口，不再是新用户的主交互面。

模型宿主与模型 Provider 必须分开配置。宿主负责人与产品的对话；Provider 是否被调用、调用哪家模型以及发送哪些字段，仍由产品引擎根据用户授权和隐私策略决定。在 ChatGPT 中使用产品不等于默认允许把原始健康数据发送给 OpenAI。

### 3. 宿主包只包含适配信息

每个宿主包可以包含安装清单、工具描述、对话指南、工作流和展示模板，但不复制训练规则或用户数据。

```text
hosts/
├── chatgpt/     # MCP/Plugin 与 Action 描述
├── workbuddy/   # MCP 插件清单与 Skill/Workflow
└── coze/        # Plugin/OpenAPI 与 Workflow 模板
```

### 4. 新手引导是应用层状态机

引导进度存在产品本身，不依赖某一段对话历史。任意宿主都通过相同工具读取当前状态和下一步：

```text
engine_ready
  → profile_required
  → data_source_required
  → automation_setup
  → syncing
  → baseline_partial | baseline_ready
  → goal_required
  → check_in_required
  → first_plan_review
  → active
```

用户换宿主、换模型或关闭对话后可继续，不会重新走一遍。

### 5. 工具返回宿主无关的结构化结果

所有用户面向的工具至少返回：

- `summary`：可由宿主直接表述的短摘要；
- `facts`：结构化事实与单位；
- `evidence_ids`：证据引用；
- `confidence` 和 `data_gaps`：置信度与缺失；
- `risk`：确定性安全结果；
- `next_actions`：下一步可调用工具；
- `requires_confirmation`：是否需要用户明确确认；
- `disclosure`：本次向模型宿主暴露的数据范围。

宿主可将同一结果渲染为文本、卡片或 Workflow 节点，但不重新计算业务结论。

### 6. 身份、授权和确认在引擎侧生效

- 使用 `installation_id + host + external_subject` 映射本地 `athlete_id`；
- 本地宿主使用一次性配对码或本机权限，远程宿主使用 OAuth/OIDC；
- Garmin Token、数据库路径和模型密钥不返回给宿主；
- 数据删除、导出、计划写回和其他可变更动作必须使用短时效确认令牌；
- 宿主的“我已确认”文本不能替代引擎侧的授权校验。

### 7. 支持三种交付模式

1. **本地模式**：数据和引擎都在用户设备，适合 Codex/WorkBuddy 等能调用本地工具的宿主。
2. **自托管远程模式**：用户部署引擎和 OAuth，供 ChatGPT/Coze 等远程宿主访问。
3. **托管模式**：商业版提供托管服务，仍使用同一工具契约和用户数据权利边界。

## 首批公共工具

```text
system.get_status
onboarding.get_state
onboarding.answer
source.list_options
source.begin_connection
source.get_sync_status
source.sync
automation.configure
automation.get_status
report.get_today
report.get_weekly
check_in.record
goal.create
goal.list
plan.create_first_week
plan.review
data.get_quality
notification.configure
notification.list_unread
data.export_preview
data.export_confirm
```

工具数量应保持少而稳定，避免模型因相似工具过多而选择错误。

## 后果

### 正面

- 用户不需要学习 CLI 或产品内部数据模型；
- 同一个账号可以在不同宿主继续引导和日常使用；
- 平台适配与训练领域规则分离；
- 当宿主改变 MCP、Plugin 或 Action 机制时，不需要迁移个人数据和核心业务。

### 代价

- 需要维护 MCP 和 HTTP/OpenAPI 两个传输适配；
- 每个宿主都需要契约测试和最小端到端验收；
- 远程模式引入身份、网络和托管成本，不能与本地模式共用默认威胁假设。

## 非目标

- 不让模型宿主直连 SQLite 或原始 Garmin 数据；
- 不把对话历史当作用户状态数据库；
- 不为每个宿主复制一套分析算法；
- 不在用户未确认时进行写回、删除或敏感数据外发。

## 官方接入依据

- OpenAI：[GPT Actions](https://developers.openai.com/api/docs/actions/introduction)、[ChatGPT 自定义 MCP](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)
- WorkBuddy：[插件系统](https://www.workbuddy.cn/docs/workbuddy/Plugins)
- Coze：[Agent/Workflow 中使用插件](https://www.coze.com/open/docs/guides/use_plugin)、[Workflow](https://docs.coze.com/guides/workflow)

平台能力需在发布宿主包时重新验证，不把当前平台限制固化为核心业务假设。
