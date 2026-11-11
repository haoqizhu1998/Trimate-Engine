# Trimate

这是一个本地优先、证据可追溯的个人训练工具。它把 Garmin 中国区或文件中的训练数据保存到用户自己的私有数据保险库，用确定性算法生成日报、周报和安全提示；AI 是可选解释层，不是核心计算依赖。

## 开源与个人数据

Trimate 的产品源码、Agent/Skill、数据库结构、合成测试和公开文档采用 [Apache-2.0 许可证](LICENSE)。全部 Agent 与 Skill 均由 Trimate 项目直接维护；版权与商标边界见[版权说明](NOTICE)。依赖项和外部设备服务各自适用其原有条款；本项目不隶属于 Garmin 或 COROS。

仓库和发布包不包含任何人的训练记录、身体数据、设备登录会话或 API 密钥。每位用户在自己的电脑上建立私有数据目录并连接自己的设备；上传源码或 ZIP 到 GitHub 不会同步用户数据。公开提 Issue 或贡献代码时请使用合成数据，参见 [贡献指南](CONTRIBUTING.md) 和 [安全说明](SECURITY.md)。

当前安装包尚未完成 Windows 实机及 WorkBuddy 新用户端到端验收；支持附件的普通 GPT 网页对话也无法单凭 ZIP 安装本机 MCP。当前可用范围与限制见下文和 [普通用户交付](docs/workbuddy-consumer-install.md)。

GPT/Codex、WorkBuddy、Coze 是平行的对话载体；佳明、高驰是平行的设备数据来源。设备连接、标准化、去重和分析属于产品通用能力，不绑在某个载体上。佳明中国区与国际区均支持本机自动入库，国际区通过每位用户自己的官方 Personal API Key 连接；高驰有官方 MCP 在线查询及试验性本机轮询，真实账号链路仍需验收。见[设备连接能力](docs/device-connections.md)。

产品提供训练与一般健康指导，不提供疾病诊断、治疗、处方或紧急监护。疼痛、疾病、肿胀或日常功能受限会触发确定性安全规则，并建议用户寻求合适的专业帮助。

## 当前状态

阶段 0–5 的本地功能和自动化测试已完成到仓库现有范围。阶段 5.5 已实现以下本地自动闭环：

- Garmin 中国区和国际区独立凭据、只读轮询；
- 私有 SQLite、内容寻址的原始证据和不可变分析快照；
- 仅在标准记录实际变化时自动运行确定性日报/周报和安全规则；
- 持久化通知 Inbox、macOS 系统通知、失败重试与崩溃恢复；
- `launchd` 用户级后台调度器，以及查询、强制运行、安装和卸载 CLI。
- 面向 GPT/Codex 等宿主的本地 stdio MCP：统一能力目录、引导与档案摘要、活动/数据质量/报告、目标和计划、Check-in、恢复/营养/赛事、成长画像和每日决策，以及受审批的手动同步与通知已读。敏感的凭据、文件路径、恢复/删除和后台安装仍只在本机管理入口。
- 私有结构化训练喜好：先预览并确认，再进入成长画像；新计划可读取偏好，单次输入优先，旧计划在偏好变化后提示复核。
- 七天训练计划参考最近四个七天窗口：连续训练且低风险时可预览条件性增量，较轻周可向稳定训练量回归；突增、中断或状态未知时维持谨慎预算。预算和实际排课分别展示，未来周仍逐周复核与确认。
- 多周专项重点轮换：预览 2–12 周方向，首周生成可确认的七天草案；赛前一周与比赛周有保守减量提示，后续周重新评估数据与安全状态后再预览、确认。
- 用户临时改课：可预览本周单节课的跳过、缩短、低强度换项目或挪动；自动同步的新活动若落在已批准计划周内，会发出复核通知，具体改动经用户确认后保存为新版本。
- 新活动与课表核对：同日或相邻一天的活动会形成唯一、跨日或有歧义的候选关系，并列出额外活动；用户核对完成状态、RPE 和疼痛后再确认写入，设备数据不会自动判定完成。
- 可连接 Codex app-server 的 MCP 重载命令：`athlete-growth mcp-reload`；桌面嵌入式会话若无控制 socket，命令会提示需要重启，详见 [GPT 对话接入](docs/chatgpt-mcp.md)。
- 高驰 COROS 官方 MCP 的中国区在线连接与本机试验性轮询见[设备连接](docs/device-connections.md)。本机同步依赖官方 OAuth helper 和机器可读的活动列表，真实账号链路尚需验收。
- 佳明国际区通过[官方 Personal API](https://developer.garmin.com/gc-developer-program/personal/personal-api/)接入；Key 只由每位用户在本机隐藏输入，连接成功后新增自动同步策略。国际区真实账号链路仍需用户自行验收。

尚未实现：ChatGPT 网页端插件的账号侧安装与远程连接、Coze 适配、HTTP/OpenAPI 传输；通用交付 ZIP 内含双平台本机引擎安装文件，但尚未完成 Windows 实机、WorkBuddy 客户端及全新机器验收，跨对话常驻仍需宿主配置；OAuth/OIDC 与官方 Garmin 商业 Push；邮件和移动端投递；Linux systemd 安装器；独立 Web/App；托管、多用户常驻服务。本地 MCP 适配器不等于 ChatGPT 插件已经安装。当前目标共创需要用户/对话宿主补充类型等字段；目标支持新增、替换、删除的预览与确认。计划支持近期训练暴露约束的 7 天排课、重点轮换预测、目标变化复核和新版草案，尚无自动渐进的多周专项处方。

本仓库没有声称已经在某位用户的 Mac 上完成 Garmin 登录或真实后台安装验证。真实使用仍需用户亲自完成登录或 Key 授权、首次检查，并明确选择是否安装 `launchd`。

本地 MCP 与 ChatGPT 私有连接方式见 [GPT 对话接入](docs/chatgpt-mcp.md)。确认类工具必须在用户看过草案并明确同意后调用；现有本地 MCP 只强制草案版本和两步流程，尚不能独立证明真人已经同意，因此不应把它当作可直接暴露公网的远程授权方案。

能力入口与风险分级见 [ADR 0009](docs/adr/0009-host-neutral-capability-catalog.md)。Codex 桌面端可从 `get_capability_catalog` 和 `get_onboarding_status` 开始，不必先学习 CLI 命令。

Trimate Agent 是项目常驻的产品对话入口。产品登记 17 个 Skill，均配有当前产品工具入口；仓库可构建 Codex 项目入口和通用 LLM 指令包；WorkBuddy 适配器保留在源码中，不是通用交付包的入口。跑姿视频分析需要本机额外安装视觉依赖和姿态模型。WorkBuddy 当前对话可读取通用包，跨对话常驻配置仍待客户端验收；Coze 适配和跨模型行为评测仍待实现。用法见 [Trimate Agent 与 Skill](docs/agent-skills.md)，设计决策见 [ADR 0012](docs/adr/0012-always-on-training-partner-entry.md)。

成长决策第一版可从对话中读取私有画像、预览并确认今天的训练方向、记录实际反馈；重复的个人反应只会产生候选适应，经用户确认后才改变低风险日的默认建议，不自动改写训练计划。边界见 [ADR 0011](docs/adr/0011-evidence-bound-growth-decisions.md)。

## 自动闭环怎样工作

```text
Garmin/设备先把数据上传到产品可访问的来源
  → 本机 Worker 按策略轮询
  → 原始证据 + 标准记录写入私有保险库
  → 只有实际变化才触发确定性分析
  → 按策略进入本地 Inbox，并可发送 macOS 系统通知
```

本地后台只在 Mac 开机且用户已登录时运行，不是全天候云服务。睡眠、关机或断网期间不会处理；Mac 唤醒、重新登录或网络恢复后，后续轮询会按配置的回看窗口补拉。默认回看 3 天，可用 `--lookback-days` 调大。

## 安装与初始化

面向朋友可从 [GitHub 预览发布页](https://github.com/haoqizhu1998/Trimate-Engine/releases/tag/V1.5.0)下载唯一的 `Trimate-V1.5.0.zip`，由 WorkBuddy 或支持附件的 GPT 对话识别入口并开始引导；本机数据连接与自动同步仍需宿主具备本机执行能力并由用户授权。包内的 Windows/Mac 安装文件及使用边界见 [普通用户交付](docs/workbuddy-consumer-install.md)。下面是源码开发安装步骤。

日报、周报和每日决策预览可展示夜间 HRV 的最近一晚与滚动对比；测量口径、样本要求和训练解释边界见 [HRV 方法](experience/docs/hrv-recovery-methodology.md)。

要求 Python 3.12 或更高版本：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
. .venv/bin/activate
```

激活后，本页后续的 `athlete-growth`、`python`、`ruff` 和 `pyright` 命令都会使用该虚拟环境。每次打开新的终端并进入项目目录后，都要重新运行 `. .venv/bin/activate`。

私有数据目录建议放在源码仓之外。省略 `--data-home` 时，程序依次使用 `ATHLETE_GROWTH_HOME` 和 `~/.athlete-growth`：

```bash
athlete-growth init --name "Demo Athlete" --timezone Asia/Shanghai --units metric
athlete-growth status
```

如果尚未建立 Garmin 中国区本地会话，再运行：

```bash
athlete-growth garmin-cn-login --email "account@example.invalid"
```

密码和 MFA 验证码只在本机隐藏输入。不要把密码、验证码或会话文件内容粘贴到聊天、日志或 Git。

国际区用户在[佳明国际区设置页](https://connect.garmin.com/app/settings/personalApiToken)创建自己的 Personal API Key，再运行 `athlete-growth garmin-global-connect`；Key 也只在本机隐藏输入，产品不会把中国区会话用于国际区。

Garmin 中国区活动同步会按活动 ID 读取完整活动页、分圈、分段、心率/功率区间及逐点详情，并把可下载的原始 FIT 压缩包保存在私有 Raw Vault。分析常用的分圈字段进入规范化记录；路线和逐点原始响应不会直接暴露给对话模型。某些活动没有设备原始文件或 Garmin 端点不可用时，记录会标记缺失部分。已完整同步且列表摘要未变的活动会跳过详情请求。

升级前已同步的旧活动仍只有汇总；要补齐已入库活动的历史详情，在本机运行：

```bash
athlete-growth sync --backfill-existing
```

如需同步未曾入库的日期，再使用 `sync --start YYYY-MM-DD --end YYYY-MM-DD`。长日期范围会按 28 天分批持久化，重复运行按活动 ID 去重。命令结果和 `automation-status` 的 `activity_detail_coverage` 会分别显示完整、部分及仅有汇总的 Garmin 活动数。历史补同步可能请求大量活动详情，耗时取决于历史活动数和 Garmin 服务响应。

## 开启并检查自动化

保存默认策略：Garmin 中国区、每 30 分钟、回看 3 天、`actionable-digest`、本地 20 点摘要、启用。

```bash
athlete-growth automation-configure
athlete-growth automation-status
athlete-growth automation-run-once --force
athlete-growth notification-list
```

确认登录、首次运行和状态都正常后，用户可明确选择安装 macOS 后台任务：

```bash
athlete-growth automation-install-macos
```

安装器创建当前用户的 `launchd` Agent；它每 5 分钟唤起一次检查，真正同步仍服从策略中的 `--interval-minutes`。暂停策略或卸载后台任务：

```bash
athlete-growth automation-configure --disable
athlete-growth automation-uninstall-macos
```

自定义示例与故障排查见 [自动化运行手册](docs/operations.md)。

## 通知策略

`automation-configure --notification-mode` 支持三种当前已实现的精确语义：

- `actionable-digest`：中/高风险立即进入投递；低风险或未知风险延后到 `--digest-hour` 指定的个人资料本地小时。当前不承诺跨多个批次合并成一条摘要。
- `all`：每个产生真实数据变化的批次都立即创建通知。
- `off`：仍保存确定性分析快照；在 `off` 期间产生的新分析不创建新通知，之前的 Inbox/历史和待投递项不会被删除，但待投递项在 `off` 期间不会发送。重新启用通知后，仍待投递且已到期的项目可以再次进入投递队列。

没有真实数据变化时，三种策略都不会重复分析或通知。系统通知投递采用至少一次语义和指数退避；极少数情况下，macOS 已接受通知但进程来不及保存回执，恢复后可能重复显示一次。

## 常用手动流程

```bash
# 文件采集与主观状态
athlete-growth import-file activities.csv
athlete-growth check-in --at 2026-10-26T08:00:00+08:00 \
  --fatigue 3 --soreness 2 --pain 0 --mood 8 --motivation 8 --rpe 5

# 确定性报告和质量检查
athlete-growth today --date 2026-10-26
athlete-growth weekly --end-date 2026-10-26
athlete-growth data-quality

# 目标与约束计划
athlete-growth goal-add --type race --title "10K" --priority 5 --deadline 2027-01-01
athlete-growth plan-create --goal-id GOAL_ID --start-date 2026-10-26 \
  --available 0:45,2:60,5:90 --unavailable 2 --max-weekly-minutes 120 --sport running

# 数据权利
athlete-growth export profile.zip
athlete-growth backup
athlete-growth restore profile.zip --target-home /path/to/new-private-vault
```

Garmin 中国区连接器读取活动详情与原始文件、每日汇总、步数、睡眠、HRV、心率、压力、身体电量和体重。它依赖本地 `python-garminconnect` 网页会话，是非官方接口，可能因服务或账户策略变化失效。国际区连接器使用佳明官方 Python Personal API 客户端，保存原始活动 FIT ZIP，并规范化可解析的分圈和健康数据；逐点数据保留在私有原始文件中。两种来源均不会写回 Garmin；文件导入仍是离线兜底。

## 验证

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
ruff check src tests scripts
pyright src
python -m compileall -q src scripts
PYTHONPATH=src python -m athlete_growth.interfaces.cli.main --help
```

测试使用合成 Fixture，不依赖个人真实数据。真实 Garmin 登录、网络可用性、通知权限和长时间后台稳定性必须在用户自己的环境中另行验收。

## 文档与边界

- [自动化运行手册](docs/operations.md)
- [新手与模型宿主引导](docs/onboarding.md)
- [技术架构](docs/architecture.md)
- [能力地图](docs/capability-map.md)
- [路线图与交付状态](docs/roadmap.md)
- [产品、研发过程与个人数据分离](docs/adr/0001-separate-product-process-and-personal-data.md)
- [一般健康、安全与 AI 边界](docs/adr/0002-general-wellness-safety-and-ai-boundaries.md)
- [可靠的自动同步、分析与通知闭环](docs/adr/0008-durable-automatic-data-loop.md)

源码、Schema、合成测试和公开技术文档属于产品仓；真实数据库、原始健康数据、会话 Token、缓存、导出、备份和日志属于被 Git 忽略的私有数据保险库。产品代码和研发流程不依赖任何个人数据，也不依赖私有产品运营材料。
