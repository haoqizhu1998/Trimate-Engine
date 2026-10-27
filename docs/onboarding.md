# 新手与模型宿主引导

## 当前产品形态

理想体验是用户在熟悉的模型宿主里说“帮我连接训练数据并开启自动分析”，宿主逐步解释、调用本地引擎并展示结果。当前仓库已有 JSON-safe Python facade、完整 CLI 和只读 stdio MCP 服务；ChatGPT 网页插件与私有隧道、WorkBuddy、Coze 及 HTTP/OpenAPI 仍未接通。

因此当前可用方式是：本地支持 stdio MCP 的宿主可调用只读报告工具；尚未安装插件的 GPT/Codex 仍可作为对话和指导入口，在本机终端执行明确命令。开始前先按 README 安装并运行 `. .venv/bin/activate`；每个新终端都要重新激活。宿主不应索取或保存 Garmin 密码、MFA、Token、SQLite 路径或原始健康数据。未来 WorkBuddy 和 Coze 适配器也必须复用同一核心，不能各自实现训练规则。

## 用户只需要理解的闭环

```text
Garmin/设备把训练上传到可访问来源
  → Mac 上的本地 Worker 轮询
  → 私有 SQLite 和 Raw Vault 保存证据
  → 只有数据真的变化才重新分析
  → 按策略显示 Inbox / macOS 通知
```

这不是云端 24 小时监控。Mac 关机、睡眠、退出登录或断网时不会处理；恢复后下一轮会按回看窗口补拉。

## 推荐的对话式流程

### 1. 先说明边界

宿主的第一条回复应讲清四件事：

- 核心分析在本机执行，AI 不是必需项；
- 私有数据默认在 `~/.athlete-growth`，不进入产品 Git；
- Garmin 中国区连接是只读的本地认证会话；
- 产品不是医疗诊断或紧急监护系统。

示例对话：

> 用户：帮我开启 Garmin 自动分析。
>
> 助手：可以。我们会先检查本地档案，再由你在终端隐藏输入 Garmin 凭据；第一次只运行一轮并查看结果，不会自动安装后台任务。确认正常后，你再决定是否安装 macOS 后台服务。

### 2. 建立或确认本地档案

如果从未初始化，终端回退命令是：

```bash
athlete-growth init --name "Demo Athlete" --timezone Asia/Shanghai --units metric
athlete-growth status
```

已有档案时不要重复初始化。自定义数据目录必须在后续每条命令中一致使用 `--data-home`，或统一设置 `ATHLETE_GROWTH_HOME`。

### 3. 由用户亲自完成 Garmin 中国区登录

如果没有有效会话：

```bash
athlete-growth garmin-cn-login --email "account@example.invalid"
```

用户只在本机终端输入密码和 MFA。助手不得要求用户把凭据粘贴到对话中，也不得读取会话文件来“验证”。当前连接器使用非官方 Garmin Connect 中国区网页会话，可能受服务或账户策略影响。

### 4. 选择自动化策略

初学者可直接采用默认值：每 30 分钟轮询、回看 3 天、可行动即时/其余本地 20 点投递。

```bash
athlete-growth automation-configure
athlete-growth automation-status
```

需要更长的断网补拉范围或不同通知行为时，再调整：

```bash
athlete-growth automation-configure \
  --interval-minutes 60 \
  --lookback-days 7 \
  --notification-mode actionable-digest \
  --digest-hour 20
```

每次配置是完整覆盖；未提供的字段会使用默认值。`--disable` 保存暂停策略。

### 5. 先运行一次，再检查

```bash
athlete-growth automation-run-once --force
athlete-growth automation-status
athlete-growth notification-list
```

助手应把结果翻译成普通语言：是否成功同步、是否有真实变化、是否生成分析、是否有未读或待投递通知。不要把“没有变化”描述成故障；也不要把缺少 Check-in 描述为恢复良好。

`--force` 只运行已启用策略，并以当前分钟作为幂等槽；同一分钟安全重试不会创建第二个相同请求。

### 6. 由用户明确决定是否安装后台任务

首次运行确认正常后再询问：

> 要不要让它在你登录这台 Mac 时后台检查？这会安装一个用户级 `launchd` Agent；Mac 关机、睡眠或未登录时不会运行，可以随时卸载。

只有用户明确同意后，才运行：

```bash
athlete-growth automation-install-macos
```

卸载命令：

```bash
athlete-growth automation-uninstall-macos
```

安装/卸载不改变 Garmin 授权，也不删除 SQLite、Raw Vault 或通知 Inbox。

### 7. 日常查看与已读

```bash
athlete-growth automation-status
athlete-growth notification-list
athlete-growth notification-list --include-read
athlete-growth notification-read NOTIFICATION_ID
```

模型宿主应优先展示“发生了什么、依据是什么、需要做什么、数据缺口是什么”，而不是先展示内部 ID。内部 ID 只在标记已读或故障追踪时使用。

## 三种通知选择怎样解释

- `actionable-digest`：中/高风险立即；普通变化等到个人资料时区的摘要小时。当前是延时通知，不保证多个批次合并成一条。
- `all`：每个真实变化批次都立即通知，可能更频繁。
- `off`：仍自动同步和分析；`off` 期间的新分析不创建新通知。已有 Inbox/历史与待投递项保留但不发送；重新启用后，仍待投递且已到期的项目可恢复投递。

没有真实变化就不重复分析或通知。外部投递会重试；极少数情况下，macOS 已显示通知但回执尚未保存，恢复后可能重复一次。

## 宿主适配原则

### GPT / Codex

已有只读 stdio MCP 适配器，可由支持本地 MCP 的宿主启动；ChatGPT 网页端仍需用户账号侧插件和私有 Tunnel 配置。当前不能把“本地 MCP 已实现”写成“GPT 插件已安装”。具体见 [GPT 对话接入](chatgpt-mcp.md)。

### WorkBuddy

未来 MCP、Skill 和 Workflow 只负责编排引导、展示状态和调用 facade。它们不得复制分析算法，也不得保存 Garmin Token。

### Coze

未来 Plugin/OpenAPI 与 Workflow 调用相同 facade。远程账号绑定需要独立 OAuth/OIDC 和身份映射；当前未实现，不应把 Token 放入 Bot 变量、Prompt 或知识库。

## 恢复话术

- **认证失败**：“请在本机重新登录，然后强制运行一轮；不要把密码或验证码发给我。”
- **断网**：“现有数据仍安全。网络恢复后会在下一轮重试；若离线超过回看窗口，先调大 `--lookback-days`。”
- **没有变化**：“来源返回的数据与当前记录一致，因此系统按设计跳过分析和通知。”
- **通知未显示**：“先检查 Inbox 和投递积压；系统横幅失败不等于分析丢失。”
- **Mac 离线**：“本地模式不会在关机或未登录时运行；需要全天候处理仍要等待自托管/托管版本。”

更完整的参数、日志和隐私路径见 [自动化运行手册](operations.md)。
