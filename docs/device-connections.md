# 设备连接是产品通用能力

## 分层

```text
用户 → GPT / Codex / WorkBuddy / Coze（对话载体）
     → Trimate Agent + 场景 Skill
     → 产品能力入口（MCP / 未来 HTTP）
     → 应用用例、私有数据与分析

佳明 / 高驰 / 运动文件（数据来源）
     → 各自的来源适配器
     → 采集、标准化、去重与质量标记
     → 同一个用户私有库
```

对话载体负责交互，不定义某个设备的专属业务规则。设备账号由每位用户独立授权；来源适配器处理区域、授权、同步游标、详情与原始文件、错误恢复和撤销。标准化后的同一活动只计入一次训练量，同时保留各来源证据。分析、计划与成长决策读取产品私有库，结果对所有载体一致。

## 当前实现边界

| 来源 | 个人连接 | 在线查询 | 自动入产品私有库 | 变化分析与通知 |
| --- | --- | --- | --- | --- |
| 佳明中国区 | 本机登录，非官方 Garmin Connect 会话 | 通过产品库查询 | 已实现本机轮询和详情补齐 | 已实现 |
| 佳明国际区 | 每位用户在本机隐藏输入官方 Personal API Key | 通过产品库查询 | 已实现本机轮询；保存原始 FIT ZIP，规范化活动和健康数据 | 复用自动分析流程；需国际区真实账号验收 |
| 高驰 | 每位用户授权官方 COROS MCP，产品本机使用官方 helper | 可在支持 OAuth 的载体中查询 | 试验性本机轮询，需机器可读活动结果及真实账号验收 | 复用自动分析流程；需真实账号验收 |
| 运动文件 | 用户本机选择 FIT/TCX/CSV/JSON | 导入后通过产品库查询 | 单文件导入已实现；高驰历史批量导入待实现 | 导入后的分析按现有用例运行 |

高驰可在 GPT、WorkBuddy 等支持官方 COROS MCP 的载体中由用户自行授权查询。此时结果属于来源在线数据，不能称为已入产品私有库或跨载体共享的产品记录。本机适配器独立于载体，调用官方 OAuth helper；按日查询活动列表、详情和分圈，写入同一私有库。若列表只返回自然语言或达到上限，同步会报错，不会猜测 ID 或静默截断。当前未下载原始 FIT，活动详情标记为 `partial`。跨佳明与高驰只对开始时间、时长、距离几乎完全一致的活动自动去重；相近但无法确认的活动仍需后续核对。具体约束见 [ADR 0014](adr/0014-coros-device-connection.md)。

## 本机佳明国际区连接

每位用户先在[佳明国际区 Personal API 设置页](https://connect.garmin.com/app/settings/personalApiToken)使用自己的国际区账号创建 Key。Key 只在用户本机终端隐藏输入；中国区登录会话和国际区 Key 分别保存在不同的私有目录。国际区使用佳明[官方 Personal API Python 客户端](https://github.com/garmin/connect-personal-api-python)，该 API 仅面向个人自用，采取拉取方式；面向多用户的托管商业服务应另行申请商业接入。

```sh
athlete-growth garmin-global-connect
athlete-growth garmin-global-status
athlete-growth sync --source garmin-global --start 2026-11-01 --end 2026-11-04
athlete-growth automation-status
```

连接成功会自动保存国际区每 30 分钟、回看 3 天的本机同步策略。已安装的后台任务会执行该新策略；未安装时可按[自动同步运行手册](operations.md)安装。首次历史补齐使用 `sync --source garmin-global`，日期分段由产品处理；重复记录不会重复计入训练量。活动原始 FIT ZIP 存于私有 Raw Vault，详情解析失败时标记为部分可用，不会声称已补齐。撤销本机连接运行 `athlete-growth garmin-global-revoke`，这也会停用国际区自动同步策略；若需在佳明端注销 Key，还要到设置页删除。

## 本机高驰连接与试验同步

在已安装产品的终端中运行以下命令。`coros-helper-install` 仅从官方 COROS-MCP 仓库下载 helper 到个人数据目录；OAuth Token 由该 helper 保存在其本机缓存，不进入产品仓。中国区账号固定走官方中国区 issuer。

```sh
athlete-growth coros-helper-install
athlete-growth coros-login-start
# 在输出的官方链接完成授权后，回到同一台电脑
athlete-growth coros-login-finish
athlete-growth sync --source coros --start 2026-10-20 --end 2026-10-20
athlete-growth automation-configure --source coros --interval-minutes 30
athlete-growth automation-run-once --force
athlete-growth automation-status
```

已安装本机后台任务时，新增的高驰订阅由同一个任务调度；没有安装时仍需运行 `athlete-growth automation-install-macos`。撤销本机高驰授权使用 `athlete-growth coros-revoke`，已有本地活动不删除。当前官方 MCP 工具示例的活动列表可能是格式化文本，因此首次真实账号试运行可能明确报出“缺少机器可读活动 ID”；这表示不能安全自动导入，需用导出文件或等待官方结构化响应，不能把该次同步当作成功。单日达到 100 条同样停止，避免漏数。

WorkBuddy 的本地 ZIP 只负责安装Trimate的宿主侧方法。它不拥有佳明或高驰账号，也不替代本机数据引擎。GPT/Codex、WorkBuddy 和其他载体调用同一个产品 MCP 时，读取的是同一份用户私有数据。
