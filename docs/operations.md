# 自动同步运行手册

本手册对应当前 CLI 的真实实现。运行示例前先按 README 安装，并在每个新终端执行 `. .venv/bin/activate`。示例默认使用 `~/.athlete-growth`；如果使用自定义保险库，请在每条命令末尾传同一个 `--data-home /path/to/private-vault`，或设置 `ATHLETE_GROWTH_HOME`。

## 1. 首次准备

仅在尚未初始化时运行：

```bash
athlete-growth init --name "Demo Athlete" --timezone Asia/Shanghai --units metric
athlete-growth status
```

仅在尚未拥有有效 Garmin 中国区本地会话时运行：

```bash
athlete-growth garmin-cn-login --email "account@example.invalid"
```

密码和 MFA 验证码由终端隐藏读取，不写进命令。请勿把凭据或会话文件内容发到聊天中。

国际区账号使用官方 Personal API：先在[国际区设置页](https://connect.garmin.com/app/settings/personalApiToken)创建 Key，再在本机运行 `athlete-growth garmin-global-connect`。Key 由终端隐藏读取，连接成功时自动新增 `garmin-global` 同步策略；中国区会话继续独立存在。

## 2. 配置策略

不带可选参数时会保存以下完整默认策略：

| 字段 | 默认值 | 含义 |
|---|---:|---|
| `--source` | `garmin-cn` | 可选 `garmin-cn`、`garmin-global` 或 `coros`；不同来源有独立策略 |
| `--interval-minutes` | `30` | 到期同步间隔 |
| `--lookback-days` | `3` | 每次包含当天在内的回看天数 |
| `--notification-mode` | `actionable-digest` | 可行动即时、其余延时 |
| `--digest-hour` | `20` | 个人资料时区的本地小时，范围 0–23 |
| 启用状态 | 启用 | `--disable` 才保存为暂停 |

```bash
athlete-growth automation-configure
```

覆盖示例：

```bash
athlete-growth automation-configure \
  --source garmin-cn \
  --interval-minutes 60 \
  --lookback-days 7 \
  --notification-mode all \
  --digest-hour 21
```

`--digest-hour-local` 是 `--digest-hour` 的同义参数。每次 `automation-configure` 都会完整覆盖当前策略；省略的字段会回到默认值。暂停并保留上例参数时，应重复参数：

```bash
athlete-growth automation-configure \
  --interval-minutes 60 \
  --lookback-days 7 \
  --notification-mode all \
  --digest-hour 21 \
  --disable
```

重新启用时运行同一命令并去掉 `--disable`。

## 3. 首次安全检查

查看策略、下次运行、最近运行、积压数和最近分析：

```bash
athlete-growth automation-status
```

先主动跑一轮，不安装后台服务：

```bash
athlete-growth automation-run-once --force
```

`automation-run-once` 不带 `--force` 时只处理到期或可恢复工作，并尝试当前到期的通知投递。`--force` 会立即处理所有已启用策略；同一分钟内重复强制运行使用同一幂等槽，不会创建第二个相同请求。纯重复来源数据不会产生变化集、分析或通知。

查看未读通知、查看全部、标记一条已读：

```bash
athlete-growth notification-list
athlete-growth notification-list --include-read
athlete-growth notification-read NOTIFICATION_ID
```

## 4. 通知模式

- `actionable-digest`：选中的中/高风险快照立即可投递；低/未知风险通知排到下一次 `--digest-hour`。如果当天该小时已过，则排到次日。当前版本不做跨变化集摘要合并。
- `all`：每个真实变化集创建一条立即可投递通知；一批变化只选择该批最需行动的快照生成通知。
- `off`：同步和确定性分析照常执行；`off` 期间产生的新分析不创建新通知。已有 Inbox/历史和待投递项会保留，不会被删除或取消，但外部投递在 `off` 期间被抑制；重新启用后，仍待投递且已到期的项目可以再次被领取。

通知先持久化，再尝试外部投递。失败后按 1、2、4、8、16、32 分钟退避，之后封顶为每小时一次。投递是至少一次语义：如果 macOS 已接受通知，而进程在保存成功回执前崩溃，租约到期后可能重放一次。

## 5. 安装或卸载 macOS 后台任务

只有完成登录、配置、`--force` 首次运行和结果检查后，才由用户明确选择安装：

```bash
athlete-growth automation-install-macos
```

安装结果应包含 `"installed": true` 和标签 `com.athlete-growth.automation`。Launch Agent 在用户登录时加载，每 300 秒运行一次到期检查；策略自己的同步间隔仍由 `--interval-minutes` 决定。

卸载不会删除策略、SQLite、原始证据或 Inbox：

```bash
athlete-growth automation-uninstall-macos
```

本地 Launch Agent 只在 Mac 开机且该用户已登录时运行。关机、睡眠、退出登录或断网期间没有云端 Worker；恢复后会在下一次检查中继续处理，并按 `--lookback-days` 补拉。因此离线时间可能超过回看窗口时，应先增大窗口，再强制运行。

## 6. 故障排查

### 认证或会话失效

1. 运行 `athlete-growth automation-status` 查看最近运行是否失败。
2. 在本机终端重新执行 `athlete-growth garmin-cn-login --email "account@example.invalid"`。
3. 再执行 `athlete-growth automation-run-once --force`。
4. 如果需要彻底撤回本地中国区会话，运行 `athlete-growth garmin-cn-revoke`，然后重新登录。

不要打印、复制或提交 `tokens/garmin-cn-session/` 中的内容。

### 断网或 Garmin 服务暂时不可用

失败运行会留在状态历史中，现有数据不会被删除；策略仍按下一个配置间隔继续尝试。网络恢复后运行：

```bash
athlete-growth automation-run-once --force
athlete-growth automation-status
```

若断网超过当前回看窗口，先用足够大的 `--lookback-days` 重新保存策略。

### 检查 launchd

```bash
launchctl print gui/"$(id -u)"/com.athlete-growth.automation
tail -n 100 ~/.athlete-growth/logs/automation.stdout.log
tail -n 100 ~/.athlete-growth/logs/automation.stderr.log
```

自定义数据目录时，日志位于该目录的 `logs/`。Launch Agent 清单位于 `~/Library/LaunchAgents/com.athlete-growth.automation.plist`。不要把整个数据保险库或 Token 文件作为故障附件；分享日志前先检查是否含个人训练信息。

### 收不到 macOS 系统通知

先确认 `notification-list` 有项目、`automation-status` JSON 中的 `backlog.delivery_backlog` 是否为零，并检查错误日志。还需在 macOS 系统设置中允许当前 Python/终端相关进程显示通知。Inbox 是本地事实源；系统横幅缺失不表示分析快照丢失。若当前策略为 `off`，非零投递积压会保留但不会发送。

## 7. 私有文件位置

默认保险库的结构是：

```text
~/.athlete-growth/
├── config.toml
├── tokens/garmin-cn-session/       # 私密会话；不要查看或分享内容
├── tokens/garmin-global-personal-api/ # 每位用户自己的国际区 Key 与短期令牌
├── profiles/<athlete-id>/
│   ├── db/athlete.sqlite3          # 标准记录、自动化状态、快照、Inbox
│   ├── raw/                        # 按内容哈希保存的原始证据
│   ├── reports/
│   └── backups/
└── logs/
```

`automation-status` 的输出刻意不包含凭据、租约、原始载荷或本地路径。上述私有目录均由 `.gitignore` 排除，但仍应使用操作系统账户权限和正常备份保护。
