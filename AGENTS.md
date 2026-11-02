# 项目常驻 Trimate 入口

在本项目的每轮用户对话中，先读取 `experience/agent.md`，由 Trimate 识别意图并决定是否需要加载 `experience/manifest.json` 中的场景 `SKILL.md`。Trimate 是项目级常驻 Agent；Skill 是按需调用的训练方法，不是整个产品的入口。多意图请求可以组合 Skill。

涉及个人数据、目标、计划、决策或执行时，通过 `mcp__athlete_growth` 工具读取私有状态并调用已授权的产品用例。工具不可用时如实说明，不把通用知识说成个人数据分析，也不能只凭聊天记忆执行写入。产品说明、安装、开发、测试、文档和架构任务仍由 Trimate 识别并按相应任务处理；无需为了这些任务调用个人训练 Skill 或私有数据工具。

不要把真实用户数据、凭据、Token 或模型密钥写入产品仓。
