# 个人运动训练分析与成长工具

这是一个本地优先、证据可追溯的个人训练工具。它把 Garmin 中国区或文件中的训练数据保存到用户自己的私有数据保险库，用确定性算法生成日报、周报和安全提示；AI 是可选解释层，不是核心计算依赖。

产品提供训练与一般健康指导，不提供疾病诊断、治疗、处方或紧急监护。疼痛、疾病、肿胀或日常功能受限会触发确定性安全规则，并建议用户寻求合适的专业帮助。

## 当前状态

阶段 0–5 的本地功能和自动化测试已完成到仓库现有范围。阶段 5.5 已实现以下本地自动闭环：

- Garmin 中国区只读轮询；
- 私有 SQLite、内容寻址的原始证据和不可变分析快照；
- 仅在标准记录实际变化时自动运行确定性日报/周报和安全规则；
- 持久化通知 Inbox、macOS 系统通知、失败重试与崩溃恢复；
- `launchd` 用户级后台调度器，以及查询、强制运行、安装和卸载 CLI。

尚未实现：GPT/WorkBuddy/Coze 的 MCP、Plugin 或 OpenAPI 传输；OAuth/OIDC 与官方 Garmin 商业 Push；邮件和移动端投递；Windows Task Scheduler 与 Linux systemd 安装器；独立 Web/App；托管、多用户常驻服务。现有 AI Provider 代码和宿主无关 Python facade 不等于这些宿主传输已经可用。

本仓库没有声称已经在某位用户的 Mac 上完成 Garmin 登录或真实后台安装验证。真实使用仍需用户亲自完成登录、首次检查，并明确选择是否安装 `launchd`。

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

Garmin 中国区连接器读取活动、每日汇总、步数、睡眠、HRV、心率、压力、身体电量和体重。它依赖本地 `python-garminconnect` 网页会话，是非官方接口，可能因服务或账户策略变化失效；文件导入仍是离线兜底。它不会写回 Garmin。

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
