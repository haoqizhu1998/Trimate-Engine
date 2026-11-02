"""Export the product Agent and Skills as a WorkBuddy expert package."""

from __future__ import annotations

import json
import re
import shutil
import struct
import zlib
from pathlib import Path

from athlete_growth.experience.bundle import EXPERIENCE, _skill_body, _source_file, load_manifest


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _avatar(path: Path) -> None:
    """Make a small, dependency-free avatar for the expert card."""
    size = 128
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            # Green field, a white track oval, and a center dot.
            oval = 0.45 < ((x - 63.5) / 49) ** 2 + ((y - 63.5) / 38) ** 2 < 0.65
            dot = (x - 63.5) ** 2 + (y - 63.5) ** 2 < 10**2
            row.extend((246, 252, 247) if oval or dot else (24, 116, 86))
        rows.append(bytes(row))
    pixels = zlib.compress(b"".join(rows), level=9)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", pixels)
        + _png_chunk(b"IEND", b"")
    )


def build_workbuddy_expert(output: Path, *, author_email: str = "contact@example.invalid") -> Path:
    """Build a WorkBuddy expert definition; no user state is ever included."""
    manifest = load_manifest()
    agent = manifest["agent"]
    skills = manifest["skills"]
    assert isinstance(agent, dict) and isinstance(skills, list)
    output = output.expanduser().resolve()
    if output.exists():
        shutil.rmtree(output)
    (output / ".codebuddy-plugin").mkdir(parents=True)
    (output / "agents").mkdir()
    (output / "avatars").mkdir()
    (output / "skills").mkdir()
    for shared in ("manifest.json", "host-contract.md"):
        source = EXPERIENCE / shared
        if source.is_file():
            shutil.copy2(source, output / shared)
    if (EXPERIENCE / "docs").is_dir():
        shutil.copytree(EXPERIENCE / "docs", output / "docs")
    _avatar(output / "avatars" / "training-partner.png")

    skill_paths = []
    for record in skills:
        assert isinstance(record, dict)
        skill_id = str(record["id"])
        source = _source_file(str(record["source"]))
        target = output / "skills" / skill_id
        shutil.copytree(source.parent, target)
        # WorkBuddy requires display metadata absent from the canonical Skill.
        body = _skill_body(source, skill_id)
        description = (
            source.read_text(encoding="utf-8").split("description: ", 1)[1].splitlines()[0]
        )
        heading = re.search(r"^# (.+)$", body, flags=re.MULTILINE)
        display_name = heading.group(1).strip() if heading else skill_id
        header = {
            "name": skill_id,
            "display_name": display_name,
            "display_name_en": skill_id.replace("-", " ").title(),
            "description": description,
            "description_zh": description,
            "description_en": f"Training method: {skill_id}",
            "version": str(record["version"]),
            "author": "Trimate",
        }
        # All values are JSON strings, valid YAML scalar syntax.
        target.joinpath("SKILL.md").write_text(
            "---\n"
            + "\n".join(
                f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in header.items()
            )
            + "\n---\n\n"
            + body,
            encoding="utf-8",
        )
        skill_paths.append(f"./skills/{skill_id}")

    agent_text = _source_file(str(agent["source"])).read_text(encoding="utf-8").strip()
    (output / "agents" / "training-partner.md").write_text(
        "---\n"
        "name: training-partner\n"
        "description: Trimate endurance training with local data.\n"
        "displayName:\n  en: Trimate\n  zh: Trimate\n"
        "profession:\n  en: Endurance training advisor\n  zh: 耐力运动训练顾问\n"
        "---\n\n" + agent_text + "\n\n## WorkBuddy 工具入口\n"
        "先调用 `get_onboarding_status` 与 `get_capability_catalog`；只有连接成功后才进行个人分析。"
        "所有个人数据和写入通过 athlete-growth MCP，凭据只在用户本机登录。"
        "按用户意图加载同包内对应的 Skill。\n",
        encoding="utf-8",
    )
    display_zh = (
        "陪伴跑步、骑行、游泳与铁三训练，依据个人数据分析状态、制定和调整计划，"
        "记录反馈并辅助比赛决策。"
    )
    plugin = {
        "name": "athlete-growth-training-partner",
        "version": str(agent["version"]),
        "description": "Trimate endurance training with private local data.",
        "author": {"name": "Trimate", "email": author_email},
        "agents": ["./agents/training-partner.md"],
        "skills": skill_paths,
        "expertType": "agent",
        "agentName": "training-partner",
        "displayName": {"en": "Trimate", "zh": "Trimate"},
        "profession": {"en": "Endurance training advisor", "zh": "耐力运动训练顾问"},
        "displayDescription": {
            "en": (
                "Uses your private training data to review progress, adjust plans, "
                "and prepare for endurance races."
            ),
            "zh": display_zh,
        },
        "avatar": "avatars/training-partner.png",
        "categoryId": "15-Education",
        "defaultInitPrompt": {
            "en": "Check my training data and help me get started",
            "zh": "检查我的训练数据，帮我开始使用",
        },
        "plugin": "athlete-growth-training-partner",
        "tags": [
            {"en": "Endurance", "zh": "耐力训练"},
            {"en": "Planning", "zh": "训练计划"},
            {"en": "Race", "zh": "比赛策略"},
        ],
        "quickPrompts": [
            {
                "en": "Check my training data and help me get started",
                "zh": "检查我的训练数据，帮我开始使用",
            },
            {"en": "Review my recent training", "zh": "复盘我最近的训练"},
            {"en": "Adjust my plan for my next race", "zh": "根据下一场比赛调整计划"},
        ],
        "dependencies": {"mcpServers": "./.mcp.json"},
    }
    (output / ".codebuddy-plugin" / "plugin.json").write_text(
        json.dumps(plugin, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    mcp = {
        "mcpServers": {
            "athlete-growth": {
                "type": "stdio",
                "command": "/bin/sh",
                "args": [
                    "-c",
                    'exec "$HOME/.local/share/athlete-growth/venv/bin/athlete-growth-mcp"',
                ],
                "x-workbuddy": {
                    "displayName": {"zh": "Trimate 本地数据", "en": "Trimate Local Data"},
                    "description": {
                        "zh": "在这台电脑上读取私有训练数据。",
                        "en": "Read private training data on this computer.",
                    },
                    "auth": {"type": "none"},
                },
            }
        }
    }
    (output / ".mcp.json").write_text(
        json.dumps(mcp, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "README.md").write_text(
        "# Trimate WorkBuddy 专家\n\n"
        "先运行用户安装包中的 `安装Trimate.command`，再在 WorkBuddy 召唤本专家。"
        "Garmin 登录由用户在本机完成。"
        "本目录是 Agent、Skill 和 MCP 的专家定义；普通用户的本地文件上传入口"
        "使用另行生成的 WorkBuddy 本地技能 ZIP。上架专家市场才涉及平台审核。\n",
        encoding="utf-8",
    )
    return output


def build_workbuddy_local_skill(output: Path, expert: Path) -> Path:
    """Wrap the product methods in the local Skill format accepted by WorkBuddy."""
    output = output.expanduser().resolve()
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    for name in ("manifest.json", "host-contract.md"):
        source = expert / name
        if source.is_file():
            shutil.copy2(source, output / name)
    shutil.copytree(expert / "skills", output / "skills")
    shutil.copytree(expert / "docs", output / "docs")
    shutil.copy2(expert / "agents" / "training-partner.md", output / "agent.md")
    (output / "SKILL.md").write_text(
        "---\n"
        'name: "athlete-growth-workbuddy"\n'
        'display_name: "Trimate · 本地包"\n'
        'description: "跑步、骑行、游泳、铁三训练分析、计划与比赛策略；'
        '按用户意图读取 Trimate Agent 与场景方法。"\n'
        'version: "1.0.0"\n'
        'author: "Trimate"\n'
        "---\n\n"
        "# WorkBuddy 本地文件适配\n\n"
        "当用户询问个人训练、身体状态、目标、计划、比赛、数据连接或产品使用时，"
        "先读取同目录 `agent.md`，由 Trimate 识别意图，再按 `manifest.json` 和 "
        "`agent.md` 路由读取 `skills/<场景 ID>/SKILL.md`。"
        "可组合多个场景方法；调用工具前先核对 `host-contract.md`。\n\n"
        "个人数据与写入只能通过用户本机的 athlete-growth MCP。"
        "先调用 `get_onboarding_status` 和 `get_capability_catalog` 确认连接；"
        "工具不可用时说明缺口，不得依据聊天历史伪造个人分析。"
        "佳明和高驰都是产品通用的数据来源，由每位用户分别授权。"
        "当前高驰官方 MCP 的在线查询结果不等于已写入本地私有库；"
        "WorkBuddy 仅承载对话，不拥有设备连接规则。\n\n"
        "本 ZIP 是 WorkBuddy 支持的本地 Skill 文件容器。产品的权威对话定义仍在 "
        "`agent.md`，17 个场景方法仍按需加载。若宿主支持把 `agent.md` 设置为常驻专家指令，"
        "应设置为常驻，以便所有对话由 Trimate 识别。\n",
        encoding="utf-8",
    )
    return output
