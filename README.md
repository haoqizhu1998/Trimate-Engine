# 个人运动训练分析与成长工具

这是一个本地优先、证据可追溯、以模型宿主为主要交互入口的个人训练工具。目标形态是在 ChatGPT/Codex、WorkBuddy、Coze 等熟悉的 AI 软件中完成安装、同步、分析、计划和反馈，训练核心与任一宿主保持解耦。

阶段 0–5 的本地代码能力已经形成完整纵向闭环：个人档案与数据权利、Garmin 中国区及文件采集、确定性分析和安全规则、目标与训练计划、多模型 AI 路由、恢复/营养/赛事策略。当前可用入口仍是 CLI，同步和报告也需要主动调用；阶段 5.5 的后台自动同步/分析/通知、模型宿主安装、对话式新手引导和 MCP/OpenAPI 适配尚未开发。阶段 6 的独立 Web/App、托管服务、商业发行和正式开源工作也没有开发。

产品只提供训练与一般健康指导，不提供疾病诊断、治疗或处方。疼痛、疾病、肿胀或日常功能受限会优先触发确定性安全规则和专业帮助建议，AI 不能绕过这些规则。

## 目标交互形态

- **ChatGPT/Codex**：通过 MCP/Plugin 接入；不具备完整 MCP 能力时可使用 HTTP/OpenAPI Action；
- **WorkBuddy**：通过 MCP 插件接入，Skill 与 Workflow 只负责对话和流程编排；
- **Coze**：通过自定义 Plugin/OpenAPI 接入，并用 Workflow 承载首次引导与固定闭环；
- **产品核心**：统一 Tool Catalog、身份授权、引导状态、训练算法和个人数据均保存在引擎侧；
- **后台闭环**：来源支持推送时接收事件，否则由本地 Worker 增量轮询；真实数据变化自动触发确定性分析，再按用户策略投递即时提醒或摘要；
- **CLI**：保留为开发、诊断、恢复和无宿主时的降级入口。

用户更换宿主或模型时不需要迁移数据。每个宿主只拿到当前操作所需的最小化结果，不直接访问 SQLite、Garmin Token 或原始数据目录。

“在哪个 AI 软件里操作”和“由哪个模型解释结果”彼此独立：即使用户从 ChatGPT 进入，也可以选择本地模型或关闭 AI 外发，确定性指标与安全规则仍在产品引擎中执行。

## 已实现能力

- 每位运动员使用独立 `athlete_id`、SQLite 数据库、原始证据目录和备份目录；
- 标准记录携带来源、原始数据哈希、质量标记、算法版本和不可变更正链；
- Garmin 中国区本地会话只读连接器，不依赖 Personal API Key，Token 与国际区隔离且可撤回；
- CSV、JSON、TCX、FIT 文件导入兜底，增量游标、幂等去重、失败重试和数据质量报告；
- 疲劳、酸痛、疼痛、情绪、意愿、疾病、RPE 与生活情境 Check-in；
- 训练负荷、强度、连续性、心率漂移，以及 HRV、睡眠、静息心率、身体电量和体重基线；
- 运动/身体/赛事目标、冲突检测、确定性日报/周报和非医疗安全升级；
- 受硬约束保护的训练计划、不可变调整版本、回滚、训练状态和确认式写回端口；
- 用户反馈、里程碑和明确标注“相关不等于因果”的个人响应学习；
- OpenAI、Anthropic、Gemini、OpenAI-compatible/本地模型端口，含同意、隐私、成本、降级、安全覆盖和审计；
- 复训分期、训练/比赛补给、带来源和授权信息的赛事导入，以及离线赛事策略文档；
- Profile 导出、校验、备份、恢复和确认删除，恢复后的敏感文件使用所有者权限。

## 安装

要求 Python 3.12 或更高版本：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

初始化私有数据目录。建议把 `--data-home` 放在源码仓之外；省略时依次使用 `ATHLETE_GROWTH_HOME` 和 `~/.athlete-growth`：

```bash
athlete-growth init --name "Demo Athlete" --timezone Asia/Shanghai --units metric
athlete-growth status
```

## 常用流程

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
athlete-growth plan-create --goal-id <goal-id> --start-date 2026-10-26 \
  --available 0:45,2:60,5:90 --unavailable 2 --max-weekly-minutes 120 --sport running

# 数据权利
athlete-growth export profile.zip
athlete-growth backup
athlete-growth restore profile.zip --target-home /path/to/new-private-home
```

运行 `athlete-growth --help` 查看恢复、营养、赛事和离线 AI 命令的全部参数。

## Garmin 中国区

中国区主路径使用本地 Garmin Connect 网页会话，不需要 Personal API Key。密码和 MFA 验证码只在本机交互式输入，产品只保存权限受限的会话 Token；不要把密码或 Token 粘贴到聊天或 Git：

```bash
athlete-growth garmin-cn-login --email "your-cn-account@example.com"
athlete-growth sync --start 2026-10-01 --end 2026-10-26
# 首次全量回填（默认每 28 天落盘一批，中断后可安全重跑）
athlete-growth sync --start 2024-06-02 --end 2026-10-26 --chunk-days 28
athlete-growth garmin-cn-revoke
```

同步覆盖活动、每日汇总、步数、睡眠、HRV、心率、压力、身体电量和体重。批次完成后立即落盘；重跑同一时间段时，来源身份和内容都未变的记录只计为重复，不会再写一份。旧格式分时片段会保留为可审计历史，不再进入当前分析视图。

连接器使用开源 `python-garminconnect` 的中国区登录能力，不把 MCP 当成 Garmin 数据采集运行时。这是非官方 Garmin Connect 接口，可能因服务变更或账户限制失效；故障时继续使用 FIT/TCX/CSV/JSON 导入，已有数据不会被覆盖。MCP 只作为模型宿主调用产品工具的一种传输方式；Garmin 凭据由引擎保管，不交给模型宿主或托管 Prompt。详见 [Garmin 中国区认证决策](docs/adr/0006-garmin-cn-session-and-optional-mcp.md)。

## 验证

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check src tests scripts
.venv/bin/pyright src
.venv/bin/python -m compileall -q src scripts
athlete-growth --help
```

测试仅使用合成 Fixture，不依赖个人真实数据。当前历史迁移审计记录为 783/783 个活动已确认、其中 127 个经本地 TCX 转换、失败 0；这与产品运行数据严格分离。

## 项目边界与文档

- [技术架构](docs/architecture.md)
- [能力地图](docs/capability-map.md)
- [产品路线图与状态](docs/roadmap.md)
- [模型宿主优先的新手引导](docs/host-first-onboarding.md)
- [产品、研发过程与个人数据分离](docs/adr/0001-separate-product-process-and-personal-data.md)
- [一般健康、安全与 AI 边界](docs/adr/0002-general-wellness-safety-and-ai-boundaries.md)
- [版本化标准记录与连接器](docs/adr/0003-versioned-canonical-records-and-connectors.md)
- [AI 隐私路由与审计](docs/adr/0004-ai-provider-privacy-routing.md)
- [确定性安全与指导边界](docs/adr/0005-deterministic-safety-and-guidance.md)
- [宿主优先的对话式产品交付](docs/adr/0007-host-first-conversational-delivery.md)
- [可靠的自动同步、分析与通知闭环](docs/adr/0008-durable-automatic-data-loop.md)

个人真实数据、密钥、Token、缓存和日志不属于产品源码。内部调研、定价、销售、Sprint 和复盘也不进入本产品仓。自动闭环、宿主接入与引导属于阶段 5.5；许可证、独立 Web/App、托管和商业发行属于阶段 6，二者当前均未开发。
