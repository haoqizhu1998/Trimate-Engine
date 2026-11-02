# ADR 0014：高驰设备数据接入

## 决定

高驰与佳明都属于产品的数据来源能力，不属于 WorkBuddy、GPT 或其他对话载体。高驰采用官方 COROS MCP 作为个人授权入口；中国大陆账号使用官方独立地址 `https://mcpcn.coros.com/mcp`，通过高驰 OAuth 页面由每位用户自行授权。支持 MCP 的载体可在线查询；产品级本机轮询适配器已作为试验能力接入。载体在线查询不替代产品本地数据库，也不在产品仓保存高驰凭据。

产品本地历史资料的可执行入口是 COROS Training Hub 的 FIT/TCX 批量导出，再由用户在本机导入。活动应保留来源、原始 FIT、时间、项目、分圈和可用指标。该导入能力需要专门处理压缩包和跨佳明/高驰的重复活动；现有 `import-file` 仅接收单个文件，不能当作高驰批量导入已完成。

每位个人用户都可以用自己的高驰账号授权官方 MCP，无需本产品或用户申请 Partner API。官方 MCP 不提供活动 webhook，但可查询活动列表、详情和 FIT；本机适配器通过官方 OAuth helper 按日拉取活动、详情与分圈，复用已有入库与自动分析。当前没有下载 FIT；若活动列表只有格式化文本、缺少稳定 ID，适配器会停止。真实账号链路、额度和服务条款仍需验收。平台级 webhook 和双向活动传输才考虑 Partner API；获得授权前不使用非官方账号密码抓取。

## 能力边界

| 能力 | 官方 COROS MCP | COROS 批量导出 | Partner API |
| --- | --- | --- | --- |
| 对话读取近期活动、睡眠、HRV 等 | 支持，用户 OAuth 授权 | 仅活动文件 | 支持 |
| 活动详情、分圈、FIT | 支持；FIT 每 24 小时每用户 50 份额度 | FIT 可保存完整原始文件 | 支持 |
| 本地历史活动一次性补齐 | 查询与 FIT 额度限制，不用作全量迁移 | 支持，由用户导出后本机导入 | 可能受历史回看窗口限制 |
| 新活动自动进入产品私有库 | 试验性本机定时拉取；需机器可读列表和真实账号验收 | 不支持 | 支持 |
| 新活动事件推送 | 不支持 | 不支持 | 支持 |
| 产品计划写回高驰 | MCP 支持部分跑步、骑行计划，但需单独明确确认 | 不支持 | 支持 |

## 产品实现顺序

1. **个人即时对话接入**：每位用户在支持 OAuth 的载体中连接官方 COROS MCP，并用自己的账号授权；WorkBuddy、GPT 等只是不同安装界面，无需平台入驻。产品 Agent 只在工具实际可用时读取，区分“高驰在线查询结果”和“本地已入库记录”。MCP 自带计划写入工具时，沿用产品预览与明确确认边界。
2. **历史导入**：实现独立的 COROS FIT/TCX 批量导入器。先预览文件数量、日期跨度、缺损、与已有来源的潜在重复；在用户确认后写入私有 Raw Vault 与规范化记录。不能仅凭同一天、同项目去重；优先使用原始 FIT 身份，无法确定时把候选交给用户决定。
3. **产品级个人自动同步**：在产品数据连接层验证官方 MCP 的个人授权会话是否适合本机后台定时拉取。若可行，实现与佳明同层的高驰 `DataSourceConnector`，将数据适配为现有标准记录，复用变化分析与通知；处理断点恢复、额度、FIT 缺失和跨佳明重复活动。所有对话载体读取同一产品库。Partner API 仅作为未来平台级推送与规模化传输的选项，不是个人连接的前提。

## 验收标准

- 高驰与佳明同一活动只计入一次训练量；两份原始证据均可追溯。
- FIT 的路线和逐点数据留在私有 Raw Vault，不直接作为模型上下文；模型只拿分析所需的结构化投影。
- OAuth 失败、额度耗尽、FIT 缺失与账号区域错误均给出可恢复状态，不将缺口当作零值。
- 断网恢复后不漏活动；来源断开后保留用户已导入的数据，可撤销授权。

## 官方依据

- [COROS MCP 开发说明](https://support.coros.com/hc/en-us/articles/53181619102996-Build-on-COROS-MCP)：单用户 OAuth、无 webhook；平台双向活动传输另见 Partner API。
- [COROS MCP 工具说明](https://github.com/coroslab/COROS-MCP/blob/main/docs/en/tools.md)：活动、分圈和 FIT 查询，FIT 额度。
- [COROS MCP 中国区地址](https://github.com/coroslab/COROS-MCP/blob/main/docs/en/getting-started.md)。
- [COROS 历史活动批量导出](https://support.coros.com/hc/en-us/articles/33125636125204-Bulk-Export-Historical-Activity-Data)。
- [COROS Partner API](https://support.coros.com/hc/en-us/articles/53181766856724-Partner-API-Access)：平台级 webhook 与活动传输能力。
