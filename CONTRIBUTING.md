# 为 Trimate 贡献

欢迎修复问题、改进文档与提交功能建议。请先阅读 [README](README.md) 中的当前能力和限制，以及 [架构](docs/architecture.md) 中的数据边界。

## 开发检查

使用 Python 3.12 或更高版本，在项目目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/ruff check src tests scripts
```

`pyright src` 可用于改动范围的类型检查；当前主分支仍有既存类型错误，暂不作为 CI 阻断条件。不要通过关闭检查来掩盖新错误。

测试和示例必须使用合成或可公开数据。不要提交真实运动记录、位置轨迹、身体数据、数据库、导出包、密码、Cookie、Token、模型密钥或包含这些信息的日志。提交前检查 `git status` 与待提交差异。涉及 Agent/Skill 的改动，请保留 `experience/manifest.json` 与宿主工具契约的一致性；三途来源文件的原文与校验清单见 [迁移说明](docs/trimate-capability-migration.md)。

如果发现可能泄露个人数据或可被利用的安全问题，请按 [安全说明](SECURITY.md) 私下报告。
