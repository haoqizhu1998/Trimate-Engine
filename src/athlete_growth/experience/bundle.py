"""Validate product-owned Agent/Skill sources and export model-host bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path

from athlete_growth.interfaces.host.catalog import CAPABILITY_VERSION
from athlete_growth.interfaces.mcp.server import tool_catalog

PACKAGE_DIR = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_DIR.parents[2]
EXPERIENCE = PACKAGE_DIR / "assets"
if not EXPERIENCE.is_dir():
    EXPERIENCE = SOURCE_ROOT / "experience"
SEMVER = re.compile(r"\d+\.\d+\.\d+\Z")
IDENTIFIER = re.compile(r"[a-z][a-z0-9-]*\Z")


def _source_file(relative: str) -> Path:
    path = (EXPERIENCE / relative).resolve()
    if not path.is_relative_to(EXPERIENCE.resolve()) or not path.is_file():
        raise ValueError(f"invalid experience source: {relative}")
    return path


def _skill_body(path: Path, expected_name: str) -> str:
    content = path.read_text(encoding="utf-8")
    parts = content.split("---\n", 2)
    if len(parts) != 3 or parts[0] != "":
        raise ValueError(f"missing Skill frontmatter: {path.name}")
    frontmatter = parts[1]
    if f"name: {expected_name}\n" not in frontmatter or "description: " not in frontmatter:
        raise ValueError(f"invalid Skill identity: {expected_name}")
    return parts[2].strip() + "\n"


def load_manifest() -> dict[str, object]:
    manifest = json.loads((EXPERIENCE / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported experience manifest")
    agent = manifest.get("agent")
    skills = manifest.get("skills")
    if not isinstance(agent, dict) or not isinstance(skills, list) or not skills:
        raise ValueError("agent and skills are required")
    if agent.get("id") != "athlete-training-partner" or not SEMVER.fullmatch(
        str(agent.get("version", ""))
    ):
        raise ValueError("invalid agent identity or version")
    if agent.get("minimum_capability_version") != CAPABILITY_VERSION:
        raise ValueError("agent capability contract does not match product catalog")
    source = agent.get("source")
    if not isinstance(source, str) or not _source_file(source).read_text(encoding="utf-8").strip():
        raise ValueError("missing agent source")
    available = {str(tool["name"]) for tool in tool_catalog()}
    provenance = EXPERIENCE / "trimate-provenance.json"
    if provenance.is_file():
        copied = json.loads(provenance.read_text(encoding="utf-8"))
        hashes = copied.get("sha256")
        if not isinstance(hashes, dict):
            raise ValueError("invalid TriMate source provenance")
        for relative, digest in hashes.items():
            if not isinstance(relative, str) or not isinstance(digest, str):
                raise ValueError("invalid TriMate source provenance")
            actual = hashlib.sha256(_source_file(relative).read_bytes()).hexdigest()
            if actual != digest:
                raise ValueError(f"TriMate source changed: {relative}")
    ids: set[str] = set()
    for skill in skills:
        if not isinstance(skill, dict):
            raise ValueError("invalid skill record")
        skill_id = skill.get("id")
        version = skill.get("version")
        source = skill.get("source")
        required = skill.get("required_tools")
        availability = skill.get("availability", "active")
        if (
            not isinstance(skill_id, str)
            or not IDENTIFIER.fullmatch(skill_id)
            or skill_id in ids
            or not isinstance(version, str)
            or not SEMVER.fullmatch(version)
            or not isinstance(source, str)
            or not isinstance(required, list)
            or availability not in {"active", "source_only"}
            or (availability == "active" and not required)
            or not all(isinstance(name, str) for name in required)
        ):
            raise ValueError("invalid or duplicate skill metadata")
        ids.add(skill_id)
        body = _skill_body(_source_file(source), skill_id)
        missing = set(required) - available
        if missing:
            raise ValueError(f"skill {skill_id} requires unavailable tools: {sorted(missing)}")
        tool_prefixes = (
            "get_", "list_", "preview_", "confirm_", "propose_",
            "assess_", "analyze_", "run_", "mark_",
        )
        mentioned = {
            name
            for name in re.findall(r"`([a-z][a-z0-9_]*)`", body)
            if name.startswith(tool_prefixes)
        }
        undeclared = mentioned - set(required)
        if undeclared:
            raise ValueError(f"skill {skill_id} mentions undeclared tools: {sorted(undeclared)}")
    return manifest


def build_bundle(output: Path) -> Path:
    """Write inspectable Codex and generic-LLM bundles without private data."""

    manifest = load_manifest()
    agent = manifest["agent"]
    skills = manifest["skills"]
    assert isinstance(agent, dict) and isinstance(skills, list)
    agent_text = _source_file(str(agent["source"])).read_text(encoding="utf-8").strip()
    output = output.expanduser().resolve()
    codex = output / "codex"
    codex_experience = codex / "experience"
    generic = output / "generic"
    legacy_skill = codex / "athlete-training-partner"
    if legacy_skill.is_dir():
        shutil.rmtree(legacy_skill)
    (codex_experience / "skills").mkdir(parents=True, exist_ok=True)
    (generic / "skills").mkdir(parents=True, exist_ok=True)
    for name in ("host-contract.md", "trimate-provenance.json"):
        shared = EXPERIENCE / name
        if shared.is_file():
            shutil.copy2(shared, codex_experience / name)
            shutil.copy2(shared, generic / name)
    if (EXPERIENCE / "docs").is_dir():
        shutil.copytree(EXPERIENCE / "docs", codex_experience / "docs", dirs_exist_ok=True)
        shutil.copytree(EXPERIENCE / "docs", generic / "docs", dirs_exist_ok=True)

    generic_routes: list[str] = []
    for record in skills:
        assert isinstance(record, dict)
        skill_id = str(record["id"])
        source = _source_file(str(record["source"]))
        target = codex_experience / "skills" / skill_id
        shutil.copytree(source.parent, target, dirs_exist_ok=True)
        shutil.copytree(source.parent, generic / "skills" / skill_id, dirs_exist_ok=True)
        status = "（原文保留，当前不可执行）" if record.get("availability") == "source_only" else ""
        generic_routes.append(
            f"- `{skill_id}`{status}: [skills/{skill_id}/SKILL.md](skills/{skill_id}/SKILL.md)"
        )

    (codex / "AGENTS.md").write_text(
        "# 项目常驻Trimate入口\n\n"
        "每轮用户对话先读取 `experience/agent.md`，再按其意图路由加载 "
        "`experience/manifest.json` 中匹配的场景 `SKILL.md`。Trimate是项目级常驻 "
        "Agent，Skill 是按需调用的方法。个人数据和写入只通过已授权的产品工具；"
        "工具不可用时如实说明。产品开发、安装和说明任务无需调用无关训练 Skill。"
        "不要把真实用户数据、凭据或密钥写入产品仓。\n",
        encoding="utf-8",
    )
    (codex_experience / "agent.md").write_text(agent_text + "\n", encoding="utf-8")
    (codex_experience / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (generic / "agent.md").write_text(
        agent_text + "\n\n## 场景方法\n\n" + "\n".join(generic_routes) + "\n",
        encoding="utf-8",
    )
    (output / "bundle.json").write_text(
        json.dumps(
            {
                "agent_id": agent["id"],
                "agent_version": agent["version"],
                "capability_version": CAPABILITY_VERSION,
                "skills": [
                    {
                        "id": record["id"],
                        "version": record["version"],
                        "availability": record.get("availability", "active"),
                    }
                    for record in skills
                    if isinstance(record, dict)
                ],
                "hosts": ["codex", "generic-llm"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path.cwd() / "dist" / "agent-bundle")
    args = parser.parse_args()
    print(build_bundle(args.output))


if __name__ == "__main__":
    main()
