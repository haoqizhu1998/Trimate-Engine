# Trimate Agent 与 Skill

产品唯一的 Agent/Skill 源定义位于 `experience/`：`agent.md` 规定Trimate路由；`manifest.json` 登记 17 个 Skill，其中 11 个直接来自三途原文，全部保留原文并接入对应工具入口。`host-contract.md` 单独说明三途 Skill 在当前产品中的工具映射和执行边界。跑姿视频分析需本机额外安装视觉依赖与模型。真实用户数据和密钥不进入这些文件。

面向马拉松、铁三、骑行和游泳的[Skill 与工具盘点及升级顺序](endurance-skill-tool-review.md)区分了已交付能力与下一轮专项建设。
三途原文与校验方式见 [三途 Skill 原文迁移](trimate-capability-migration.md)。
跑姿视频的本机安装与异步分析流程见 [跑姿视频执行工具](running-form-tool.md)。

本项目的 `AGENTS.md` 是 Codex 的**常驻项目入口**：每轮先读取产品 Agent，再根据意图按需加载场景 Skill。产品开发或使用说明仍由 Agent 识别，但不调用无关的训练流程。当前 MCP 工具是实际数据与用例入口，Agent/Skill 不能绕过其权限和确认流程。

## 构建可检查的宿主包

安装项目后可从任意目录运行 `athlete-growth-agent-bundle --output <目标目录>`；如果项目在此功能加入前已安装，请先重新运行 README 的 `pip install -e '.[dev]'` 以注册新命令。在源码仓中也可运行：

```bash
.venv/bin/python scripts/build_agent_bundle.py
```

构建器先检查版本、Skill 身份和必需工具是否存在，再在被 Git 忽略的 `dist/agent-bundle/` 输出：

- `codex/AGENTS.md` 和 `codex/experience/`：可放入目标项目根目录的常驻 Codex 入口、Agent、17 个 Skill 及三途原版的参考资料。若目标项目已有 `AGENTS.md`，应合并入口指令，避免覆盖原有规则。本仓库已有入口，无需另装顶层Trimate Skill。
- `generic/agent.md` 和 `generic/skills/`：通用 LLM 指令源。宿主适配器须把 Agent 装入每轮产品对话的常驻指令，并按需提供场景方法；这不是已安装的 WorkBuddy/Coze 连接器。
- `bundle.json`：Agent、Skill 和能力目录版本，供宿主包声明兼容性。

产品 wheel 配置会把 `experience/` 一并打包；已安装版本的导出命令从包内资源读取同一份定义。源码仓运行时从仓库的 `experience/` 读取，便于开发和评测。`scripts/build_consumer_release.py` 只把通用 Agent/Skill 定义与本机引擎放入唯一的 Trimate 发布 ZIP；WorkBuddy 适配器仍留在源码中，不随通用发布包安装。跨对话常驻需要宿主提供持久指令或插件入口。

复制宿主包不会自动建立 MCP 连接、身份授权或后台同步。使用个人数据前必须先配置宿主可调用的产品工具；写入仍需展示预览并取得用户明确确认。当前项目 Codex 的本地 MCP 连接方式见 [GPT 对话接入](chatgpt-mcp.md)。架构修订见 [ADR 0012](adr/0012-always-on-training-partner-entry.md)。

## 更新与验收

产品独有 Skill 的方法变更需更新 `experience/manifest.json` 中对应版本；三途 Skill 如需升级，应从三途项目重新原样复制，并更新 `trimate-provenance.json`，不要在本仓库改写原文。工具适配只改 `host-contract.md` 与产品工具。改变共同路由时更新 Agent 版本。`tests/unit/test_agent_bundle.py` 检查原文、参考资料、产物完整性和工具契约。跨模型真实对话的行为评测、WorkBuddy 客户端本地导入验收和 Coze 安装适配仍在路线图中。
