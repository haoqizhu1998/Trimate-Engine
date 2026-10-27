# GPT 对话接入：本地只读 MCP

产品已提供本地 stdio MCP 服务，但不会自动向 ChatGPT、Codex 或其他宿主公开。它只提供五项只读工具：`get_sync_status`、`get_today_report`、`get_weekly_report`、`list_goals`、`list_notifications`。不提供 Garmin 密码、Token、原始证据、数据目录参数、删除和写入工具。

## 本机验证

安装项目后，可把以下命令作为支持 stdio MCP 的宿主启动命令：

```text
/绝对路径/项目/.venv/bin/python -m athlete_growth.interfaces.mcp.server
```

服务从 `ATHLETE_GROWTH_HOME` 读取私人数据目录，未设置时使用 `~/.athlete-growth`。MCP 标准输入和输出只传 JSON-RPC；凭据绝不能放在启动参数、聊天或仓库中。

## ChatGPT 网页对话

ChatGPT 网页端的自定义 MCP 插件需要账号和工作区允许该功能。私人 Mac 数据不应以无认证的公网地址提供。推荐使用 OpenAI 的 Secure MCP Tunnel：它由本机向 OpenAI 建立出站连接，能转发本地 stdio MCP，而无需开放入站端口。

1. 用户在自己的 ChatGPT 和 OpenAI Platform 账号完成登录，确认插件与 Tunnel 权限。
2. 在 Platform Tunnel 设置中创建与目标 ChatGPT 工作区关联的 Tunnel，并创建只具有所需权限的运行密钥。密钥只在本机安全输入，不发到聊天。
3. 安装官方 `tunnel-client`，以本页的本机 Python 命令配置 `--mcp-command`；使用 `doctor --explain` 验证，再运行客户端。
4. 在 ChatGPT 插件页面添加自定义 MCP，连接方式选择 Tunnel，选中对应 Tunnel。安装后在对话框中用 `@` 选择插件，并调用 `get_sync_status` 做首次验证。

账号插件安装和 Tunnel 客户端都未完成时，不能声称“GPT 对话已可直接调用”。Tunnel 客户端停止、Mac 关机/睡眠或退出登录时，远程对话不能访问本机工具；本地 `launchd` 自动同步与此独立。

连接后，用户在对话中调用日报、周报等工具时，工具返回的个人训练与身体指标会送达所选模型宿主以生成回答。只有在用户理解并同意此范围后才应安装连接。原始 Garmin Token 和证据文件不会通过这些工具返回。

官方文档：[自定义 MCP 插件](https://developers.openai.com/api/docs/guides/custom-mcp-server)、[Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)。
