# GPT 对话接入：本地 MCP

结构化训练喜好保存在用户私有库中：`get_training_preferences` 读取当前确认版本，`preview_training_preferences` 展示变更，`confirm_training_preferences` 在用户明确批准后保存；`get_growth_profile` 展示其画像投影。计划预览会在本次参数未指定时读取这些喜好，并列出来源；传 `use_saved_preferences=false` 可只使用本次输入。自由文本偏好笔记不会自动驱动排课。

产品已提供本地 stdio MCP 服务，但不会自动向 ChatGPT 网页端或其他宿主公开。当前项目的 Codex 桌面端已经完成本机原生连接验证。先用 `get_capability_catalog` 发现当前工具与本机管理边界，再用 `get_onboarding_status` 判断下一步。只读查询覆盖档案、活动、数据质量、日报/周报、分项目训练负荷、近期主观记录、目标、计划、成长历史、决策画像与历史、同步状态和通知。纯计算预览覆盖恢复、补给、赛事策略、个人响应样本和每日训练选择；目标、计划、Check-in、每日决策与候选适应采用预览或读取后明确确认，通知已读与手动同步是受审批写入工具。服务不提供 Garmin 密码、Token、原始证据、数据目录参数、任意文件路径、数据删除或 Garmin 写回。

目标/计划草案和正式记录保存在用户私有 SQLite 中，不存在模型对话历史里。`propose_goal` 保留用户原话，缺少目标类型时返回待补字段；已确认目标可用 `preview_goal_change` / `confirm_goal_change` 替换或删除，历史保留。`preview_plan` 使用近期训练时长约束的 7 天排课器；单项目用 `sport`，多项目用 `sports`，可选专项重点与关键课偏好日，预览会列出未排到的项目，数据不完整时保守降级。`preview_plan_blocks` 接收 `weeks`（2–12）和可选 `focus_sequence`，仅首周生成可确认的七天草案，未来周只给重点项目预测；未给顺序时需有已确认的 `focus_blocks` 私有偏好。`review_plan` 识别目标或新数据变化、活动与原计划的可能偏差以及下一板块到期；活动匹配只是候选，需由用户核对。用户临时改变尚未发生的一课时，`preview_plan_adjustment` 可预览 `skip`、`shorten`、`substitute` 或 `move`，不自动补课或增加训练量，用户看过差异后用 `confirm_plan` 保存新版本。`preview_plan_update` 复用旧约束并针对下一周重新评估后生成新版差异草案。高风险状态不生成可确认计划；如果预览后数据变化或跨过安全评估日期，必须重新预览。`preview_check_in` 返回绑定当前档案及输入内容的摘要，`confirm_check_in` 仅保存完全相同的内容并即时重新计算当日报告；这仍不是独立真人授权。所有 `confirm_*` 只能在用户看过内容并明确同意后调用。

自动同步的新活动若落在尚未结束的已批准计划周内，本地通知 Inbox 会产生“计划需要复核”提醒。提醒不会自动把活动归入某节课，也不会自动更改计划；Trimate读取 `review_plan` 后与用户核对执行情况，再预览后续改课。

新增的 `analyze_session`、`get_sport_baseline` 和 `compare_plan_actual` 可做单次训练与指定课表的描述性复盘；`preview_race_segments` 和 `preview_fueling_logistics` 只核算用户提供的赛事与补给输入，不推断个人能力或验证赛事资料。

三途周期总结可用 `get_periodic_training_summary` 查询，并用 `render_periodic_training_poster` 返回 PNG。手动训练记忆使用 `preview_training_memory` → 展示全部字段 → 用户确认 → `confirm_training_memory`；`list_training_memory` 可复查。跑姿视频先通过本机 `running-form-stage` 命令暂存，再在对话中用任务 ID 调用 `get_running_form_runtime`、`start_running_form_analysis`、`get_running_form_job`，必要时读取 `get_running_form_contact_sheet`；MCP 不接受任意文件路径。安装步骤见 [跑姿视频执行工具](running-form-tool.md)。

## 本机验证

安装项目后，可把以下命令作为支持 stdio MCP 的宿主启动命令：

```text
/绝对路径/项目/.venv/bin/python -m athlete_growth.interfaces.mcp.server
```

服务从 `ATHLETE_GROWTH_HOME` 读取私人数据目录，未设置时使用 `~/.athlete-growth`。MCP 标准输入和输出只传 JSON-RPC；凭据绝不能放在启动参数、聊天或仓库中。

## Codex 桌面端（当前项目）

在受信任的项目中，Codex 可读取项目级 `.codex/config.toml`。本机已为此项目配置 `athlete_growth` stdio MCP，启动本项目虚拟环境中的服务，默认读取 `~/.athlete-growth`。该配置含本机绝对路径，已加入 `.gitignore`，不随产品开源。迁移到另一台电脑时应重新安装项目，并在当地重新生成此配置。

修改工具代码后，重启 Codex 桌面端或重新打开项目任务，在输入框键入 `/mcp` 检查新工具是否加载。当前本机项目配置将写入工具设为审批模式；`confirm_goal`、`confirm_plan`、`confirm_check_in`、`confirm_training_decision` 和 `confirm_growth_adaptation` 都必须在用户看到完整内容后得到明确同意。`record_decision_outcome` 只能记录用户实际报告的结果。仅看到服务配置或 `codex mcp list` 中的条目，不代表当前已打开的对话已加载新工具。若使用 ChatGPT 网页端，仍需完成下方的独立插件/Tunnel 接入。

当 Codex 运行在**可连接的 app-server daemon** 上时，可运行 `athlete-growth mcp-reload`；该命令通过 `codex app-server proxy` 请求官方 `config/mcpServer/reload`，然后在新一轮对话检查工具是否出现。若 daemon 使用自定义控制 socket，传 `--socket /绝对路径/控制.sock`。命令成功只表示 app-server 接受重载请求，不证明当前对话已获得新工具。**当前 Codex 桌面端的嵌入式会话没有可连接的控制 socket**，命令会明确报错，仍需退出并重新打开应用或项目任务。产品 MCP 服务本身无法强制宿主重新发现工具，也不使用删除/重加配置来伪造重载。

## ChatGPT 网页对话

ChatGPT 网页端的自定义 MCP 插件需要账号和工作区允许该功能。私人 Mac 数据不应以无认证的公网地址提供。推荐使用 OpenAI 的 Secure MCP Tunnel：它由本机向 OpenAI 建立出站连接，能转发本地 stdio MCP，而无需开放入站端口。

1. 用户在自己的 ChatGPT 和 OpenAI Platform 账号完成登录，确认插件与 Tunnel 权限。
2. 在 Platform Tunnel 设置中创建与目标 ChatGPT 工作区关联的 Tunnel，并创建只具有所需权限的运行密钥。密钥只在本机安全输入，不发到聊天。
3. 安装官方 `tunnel-client`，以本页的本机 Python 命令配置 `--mcp-command`；使用 `doctor --explain` 验证，再运行客户端。
4. 在 ChatGPT 插件页面添加自定义 MCP，连接方式选择 Tunnel，选中对应 Tunnel。安装后在对话框中用 `@` 选择插件，并调用 `get_sync_status` 做首次验证。

账号插件安装和 Tunnel 客户端都未完成时，不能声称“GPT 对话已可直接调用”。Tunnel 客户端停止、Mac 关机/睡眠或退出登录时，远程对话不能访问本机工具；本地 `launchd` 自动同步与此独立。

连接后，用户在对话中调用日报、周报或计划工具时，返回的个人训练与身体指标、目标和计划草案会送达所选模型宿主以生成回答。只有在用户理解并同意此范围后才应安装连接。原始 Garmin Token 和证据文件不会通过这些工具返回。

当前本地 MCP 用草案 ID 与版本防止误确认旧草案，但没有独立的真人确认通道，也没有远程用户身份授权；宿主可能主动调用确认工具。因此本页的远程连接步骤仅是技术接入说明，不能等同于写入工具已经满足远程商用安全要求。远程模式开放确认工具前，还需实现与用户身份绑定的授权和独立确认机制。

官方文档：[自定义 MCP 插件](https://developers.openai.com/api/docs/guides/custom-mcp-server)、[Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)。
