# ADR 0008：可靠的自动同步、分析与通知闭环

- 状态：已接受
- 日期：2026-10-26

## 背景

产品需要在用户不主动输入命令的情况下完成两件事：

1. 当 Garmin 或其他设备来源出现新数据时，自动同步到该用户自己的数据库；
2. 当用户数据库出现新增、修正或失效的数据时，自动重新分析，并通过用户选择的模型宿主或其他通知渠道告知用户。

“设备产生数据”不等于产品能直接收到手表事件。Garmin 设备通常先把数据同步到 Garmin Connect，之后第三方产品才能读取。Garmin Personal API 当前只提供拉取；Garmin 商业 Activity/Health API 才提供 Push 或 Ping/Pull 交付。中国区本地会话连接器也没有可依赖的官方 webhook。因此产品承诺从“数据已进入可访问来源”开始计算同步时效，并按连接器能力选择 Push、Poll 或 Manual。

## 决策

### 1. 采用能力驱动的混合触发

每个连接器声明 `delivery_mode`、建议轮询间隔、速率限制和可回填窗口：

- `push`：校验来源签名后写入同步信号，不在 webhook 请求中执行完整分析；
- `poll`：后台运行器按连接器策略增量拉取；Garmin Personal API 和当前 Garmin 中国区走此路径；
- `manual`：文件导入或用户显式同步仍可产生相同的后续事件。

本地模式只在用户设备开机且后台运行器存活时工作；设备唤醒或网络恢复后必须自动补拉。需要全天候时效的用户可选择自托管或未来托管模式。

轮询频率不是领域常量，必须服从来源速率限制并向用户显示。后台运行器健康时，产品目标是在一个已展示的轮询周期内发现可访问的新数据，在成功落库后 2 分钟内完成核心分析；支持主动通知的健康渠道应在分析后 2 分钟内投递可行动结果，或明确显示其摘要发送时间。

### 2. 同步、分析和通知使用持久化事件串联

```text
Push / Poll / Manual
  → SyncRequest
  → Connector 增量同步
  → Raw + Canonical + Cursor
  → DataChangeSet
  → Deterministic Analysis + Safety
  → AnalysisSnapshot
  → NotificationDecision
  → NotificationOutbox
  → Host / System / Email Adapter
```

任何一步失败都不得回滚已安全落盘的上一步。运行器重启后从持久化状态继续，而不是依赖进程内队列或模型对话历史。

### 3. 使用至少一次处理与全链路幂等

外部推送、轮询和通知可能重复，因此不追求不可验证的“恰好一次”，而采用至少一次处理加幂等消费者：

- `SyncRequest`：按 `athlete_id + source + cursor/window + reason` 去重；
- `DataChangeSet`：只包含新增、替换和失效的标准记录版本；纯重复批次不触发分析；
- `AnalysisSnapshot`：按 `athlete_id + change_watermark + algorithm_version + analysis_kind` 唯一；
- `Notification`：按 `analysis_snapshot_id + policy_version + channel` 唯一；
- 投递失败可重试，成功回执不会因重放再次通知。

### 4. 新数据先经过确定性分析

- 新数据落库后自动运行数据质量、训练分析、恢复基线和安全规则；
- AI 只负责可选的自然语言解释，不负责判断是否有新数据，也不能阻断确定性结果；
- AI 不可用时仍生成结构化分析和安全提醒；
- 更正、来源冲突或记录失效必须使受影响日期范围的分析失效并重算；
- 同一批数据可合并为一次分析，避免活动、睡眠和心率分批到达时连续刷屏。

### 5. 分析完成不等于立即打扰用户

每次有效变化都生成可审计的分析快照和通知项；投递策略由用户控制：

- `actionable`：安全风险、今日计划需要改变、目标明显偏离时立即通知；
- `digest`：普通新数据进入当日摘要；
- `all`：每个有效批次都发送完成通知；
- `off`：不主动推送，但保留产品内未读收件箱。

默认采用 `actionable + daily digest`：需要行动的变化即时提醒，其余变化在当日摘要中确认已经完成同步和分析。用户可切换为每批通知或完全关闭主动推送。通知支持安静时段、频率上限、去重、合并和已读回执。高风险训练信号可以绕过普通摘要，但不能声称是医疗紧急监测。

### 6. 模型宿主不是可靠消息队列

不是所有模型宿主都允许应用在无活跃对话时主动发消息。因此：

- 产品引擎持久化 Notification Inbox，宿主每次打开或恢复会话时读取未读项；
- 支持主动回调的宿主使用对应通知适配器；
- 不支持主动回调时可使用本机系统通知、邮件或未来移动端通知作为替代；
- 通知正文只包含最小化摘要和证据引用，原始健康数据仍保留在引擎侧。

### 7. 用户可以看见并控制自动化

用户可以在任意宿主中查询：后台运行器是否在线、上次成功同步、下次计划同步、积压任务、上次分析、通知策略和失败原因。用户可以暂停自动同步、暂停某个来源、修改通知策略或立即重试；暂停不会删除已有数据。

## 持久化状态

新增以下概念实体：

- `SyncSubscription`：来源、模式、频率、启用状态和时效目标；
- `SyncRun`：触发原因、时间窗、游标、结果和错误；
- `DataChangeSet`：本批真正发生变化的记录版本与影响范围；
- `AnalysisRun` / `AnalysisSnapshot`：输入水位、算法版本、状态和输出；
- `NotificationPolicy`：渠道、级别、安静时段和频率；
- `NotificationOutbox` / `NotificationDelivery`：消息、尝试次数、回执和已读状态。

## 运行与恢复要求

- 本地安装提供后台运行器，由 macOS launchd、Windows Task Scheduler 或 Linux systemd 启动；
- 自托管/托管环境使用同一 Worker 接口，可由容器或平台任务调度器运行；
- 网络失败使用指数退避和随机抖动，认证失效停止重试并要求用户重新授权；
- 运行器更新或崩溃后不得丢失游标、分析任务或通知；
- 所有任务带相关 ID，可从通知追溯到分析、变化集、标准记录和原始证据。

## 非目标

- 不声称在手表传感器产生数据的瞬间完成同步；
- 不把 Garmin 或模型宿主当作本产品的任务状态数据库；
- 不因没有 AI 模型而停止同步、安全分析或风险提醒；
- 不对每个细粒度健康采样都发送即时通知；
- 不把普通训练风险通知描述为医疗监护或紧急救援。

## 官方依据

- Garmin Personal API：[Program Overview](https://developer.garmin.com/gc-developer-program/overview/)（Personal 交付为 Pull only）
- Garmin Activity API：[Activity API](https://developer.garmin.com/gc-developer-program/activity-api/)（商业接入可选 Push 或 Ping/Pull）
- Garmin Health API：[Health API](https://developer.garmin.com/gc-developer-program/enterprise/health-api/)（商业接入可选 Push 或 Ping/Pull）

平台能力、频率限制和授权范围必须在连接器发布前重新验证，不能把当前接口行为写死在领域层。
