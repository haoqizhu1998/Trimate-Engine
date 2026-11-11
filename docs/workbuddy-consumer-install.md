# Trimate 普通用户交付

## 唯一交付文件

给朋友发送 [GitHub 预览发布页](https://github.com/haoqizhu1998/Trimate-Engine/releases/tag/V1.5.0)的 `Trimate-V1.5.0.zip`；本地构建时位于 `dist/consumer/`。版本号来自 `packaging/release-version.txt`；下一版会生成 `Trimate-VX.Y.Z.zip`。构建脚本验证新包完整性后，只保留当前版本的 Trimate ZIP。

朋友把 ZIP 作为附件发送到支持本机文件与命令的 WorkBuddy 或 GPT/Codex 本地 Work 对话，再说“开始使用 Trimate”。`00-请AI先读.md` 是 AI 入口；`product/` 包含常驻 Agent、17 个场景 Skill 和能力清单。AI 先运行本机自动安装入口：建立默认空档案，配置当前宿主的 MCP，随后核验工具是否真的加载。压缩包没有个人数据或设备凭据，不能仅凭附件声称已连接佳明、高驰或开启自动同步。

ZIP 内保留 `install/macOS/` 和 `install/Windows/` 本机引擎安装文件及 `bootstrap-local.py`。发布包不携带 WorkBuddy 专属 Skill。自动入口只为实际宿主配置 Trimate MCP；重复运行可复用既有档案，Codex 同名冲突时保留原配置。用户仍需审核系统操作，完成设备登录、连接器信任和必要授权。普通 GPT 网页对话上传 ZIP 不会安装本机 MCP 或持续同步。

WorkBuddy [任务对话文档](https://www.workbuddy.cn/docs/workbuddy/Conversation)确认对话支持 ZIP 附件；[权限文档](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Permission-Modes)说明执行脚本和敏感操作可能需要用户确认。OpenAI [使用文档](https://learn.chatgpt.com/docs/use-chatgpt)说明附件和本地 Work 的能力边界；[MCP 文档](https://learn.chatgpt.com/docs/extend/mcp)说明网页端通过插件使用远程 MCP，不读取本机配置。普通 GPT 对话是否能读取此 ZIP，仍须在朋友的目标账号上实测。

## 构建与验证

```bash
uv build --wheel --out-dir dist/wheels
.venv/bin/python scripts/build_consumer_release.py \
  --wheel dist/wheels/athlete_growth-0.6.0-py3-none-any.whl
```

构建器只读取产品 wheel、Agent/Skill 定义与公开安装文件，不读取用户私有目录。内部引擎版本由 `pyproject.toml` 管理，当前是 `0.6.0`；对外 ZIP 的发布版本是 `1.5.0`。自动入口的配置保护和 ZIP 内容已通过本地测试；Mac/Windows 朋友实机和 WorkBuddy 客户端端到端安装仍待验证。

可在 macOS 用临时用户目录运行发布包验收：

```bash
.venv/bin/python scripts/smoke_consumer_release.py dist/consumer/Trimate-V1.5.0.zip
```

该检查会解压 ZIP、实际安装本地引擎、重复初始化、写入隔离的 Codex/WorkBuddy MCP 配置，并启动打包后的 MCP 调用新手引导。它需要联网下载公开依赖，不访问或修改现有个人档案；仍不能替代朋友的 Windows、WorkBuddy 客户端和设备账号验收。
