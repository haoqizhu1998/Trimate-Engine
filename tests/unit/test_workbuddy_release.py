from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from athlete_growth.experience.bundle import load_manifest
from athlete_growth.experience.workbuddy import (
    build_workbuddy_expert,
    build_workbuddy_local_skill,
)


class WorkBuddyReleaseTests(unittest.TestCase):
    def test_optional_coros_connection_uses_official_china_mcp(self) -> None:
        path = Path(__file__).parents[2] / "packaging" / "connectors" / "coros-cn-mcp.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            config["mcpServers"]["coros-cn"]["url"],
            "https://mcpcn.coros.com/mcp",
        )
        self.assertNotIn("token", path.read_text(encoding="utf-8").lower())

    def test_expert_contains_agent_skills_and_local_mcp(self) -> None:
        manifest = load_manifest()
        with tempfile.TemporaryDirectory() as tmp:
            root = build_workbuddy_expert(Path(tmp) / "expert")
            plugin = json.loads((root / ".codebuddy-plugin" / "plugin.json").read_text())
            self.assertEqual(plugin["expertType"], "agent")
            self.assertEqual(plugin["agentName"], "training-partner")
            self.assertEqual(plugin["author"]["email"], "contact@example.invalid")
            self.assertEqual(len(plugin["skills"]), len(manifest["skills"]))
            self.assertEqual(plugin["dependencies"]["mcpServers"], "./.mcp.json")
            avatar = (root / "avatars" / "training-partner.png").read_bytes()
            self.assertTrue(avatar.startswith(b"\x89PNG"))
            agent = (root / "agents" / "training-partner.md").read_text(encoding="utf-8")
            self.assertIn("name: training-partner", agent)
            self.assertIn("# Trimate Agent", agent)
            self.assertIn("get_onboarding_status", agent)
            self.assertTrue((root / "host-contract.md").is_file())
            self.assertTrue((root / "docs" / "endurance-training-methodology.md").is_file())
            for skill in manifest["skills"]:
                path = root / "skills" / skill["id"] / "SKILL.md"
                self.assertTrue(path.is_file(), skill["id"])
                self.assertIn("display_name:", path.read_text(encoding="utf-8"))
            mcp = json.loads((root / ".mcp.json").read_text())
            server = mcp["mcpServers"]["athlete-growth"]
            self.assertEqual(server["command"], "/bin/sh")
            self.assertIn("athlete-growth-mcp", server["args"][-1])
            self.assertNotIn(str(Path.home()), json.dumps(mcp))

    def test_local_skill_has_root_skill_and_preserves_scene_methods(self) -> None:
        manifest = load_manifest()
        with tempfile.TemporaryDirectory() as tmp:
            expert = build_workbuddy_expert(Path(tmp) / "expert")
            local = build_workbuddy_local_skill(Path(tmp) / "local", expert)
            self.assertTrue((local / "SKILL.md").is_file())
            self.assertIn("读取同目录 `agent.md`", (local / "SKILL.md").read_text())
            self.assertIn("# Trimate Agent", (local / "agent.md").read_text())
            self.assertEqual(
                {path.parent.name for path in (local / "skills").glob("*/SKILL.md")},
                {skill["id"] for skill in manifest["skills"]},
            )
            from scripts.build_consumer_release import _zip_contents

            archive_path = Path(tmp) / "skill.zip"
            _zip_contents(local, archive_path)
            with zipfile.ZipFile(archive_path) as archive:
                self.assertIn("SKILL.md", archive.namelist())
                self.assertIn("agent.md", archive.namelist())
                self.assertNotIn(".mcp.json", archive.namelist())

    def test_wheel_allowlist_rejects_unexpected_files(self) -> None:
        from scripts.build_consumer_release import _validate_product_wheel

        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "athlete_growth-0.1.0-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("athlete_growth/experience/assets/agent.md", "agent")
                archive.writestr("profiles/private.db", "private")
            with self.assertRaisesRegex(ValueError, "unexpected wheel member"):
                _validate_product_wheel(wheel)

    def test_single_bundle_contains_host_neutral_agent_and_both_installers(self) -> None:
        from scripts.build_consumer_release import _product_version, _release_version, build_release

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product_version = _product_version()
            release_version = _release_version()
            wheel = root / f"athlete_growth-{product_version}-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("athlete_growth/experience/assets/agent.md", "agent")
            (root / "out").mkdir()
            (root / "out" / "Trimate-V0.0.1.zip").write_bytes(b"old release")
            outputs = build_release(root / "out", wheel)
            self.assertEqual(set(outputs), {"ai_conversation"})
            bundle = outputs["ai_conversation"]
            self.assertEqual(bundle.parent, (root / "out").resolve())
            self.assertEqual(bundle.name, f"Trimate-V{release_version}.zip")
            self.assertEqual(list(bundle.parent.iterdir()), [bundle])
            with zipfile.ZipFile(bundle) as archive:
                names = set(archive.namelist())
                self.assertIn("00-请AI先读.md", names)
                self.assertIn("00-给用户看.md", names)
                self.assertIn("宿主连接.md", names)
                release = json.loads(archive.read("release.json"))
                self.assertEqual(release["release_version"], release_version)
                self.assertEqual(release["engine_version"], product_version)
                self.assertIn("product/agent.md", names)
                self.assertIn("product/manifest.json", names)
                self.assertFalse(any("WorkBuddy-Skill.zip" in name for name in names))
                self.assertFalse(any("workbuddy-mcp.json" in name for name in names))
                self.assertIn("install/macOS/安装Trimate.command", names)
                self.assertIn("install/Windows/Install-Windows.cmd", names)
                self.assertIn("install/macOS/bootstrap-local.py", names)
                self.assertIn("install/Windows/bootstrap-local.py", names)
                self.assertIn("install/macOS/Trimate常驻指令.md", names)
                self.assertIn("install/Windows/Trimate常驻指令.md", names)
                self.assertIn(f"install/macOS/{wheel.name}", names)
                self.assertIn(f"install/Windows/{wheel.name}", names)
                self.assertTrue(archive.read("install/Windows/Install-Windows.ps1").startswith(b"\xef\xbb\xbf"))
                self.assertIn(
                    "--automated",
                    archive.read("install/macOS/安装Trimate.command").decode("utf-8"),
                )
                self.assertIn("-Automated", archive.read("宿主连接.md").decode("utf-8"))
                manifest = json.loads(archive.read("product/manifest.json"))
                self.assertEqual(
                    {p.removeprefix("product/skills/").removesuffix("/SKILL.md") for p in names
                     if p.startswith("product/skills/") and p.endswith("/SKILL.md")},
                    {skill["id"] for skill in manifest["skills"]},
                )
                entry = archive.read("00-请AI先读.md").decode("utf-8")
                self.assertIn("压缩包不含用户数据", entry)
                self.assertIn("普通 GPT 网页对话", entry)
                self.assertFalse(any(".athlete-growth" in name for name in names))
                self.assertIsNone(archive.testzip())

    def test_release_rejects_wheel_with_wrong_product_version(self) -> None:
        from scripts.build_consumer_release import build_release

        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "athlete_growth-9.9.9-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("athlete_growth/experience/assets/agent.md", "agent")
            with self.assertRaisesRegex(ValueError, "expected an athlete-growth"):
                build_release(Path(tmp) / "out", wheel)


if __name__ == "__main__":
    unittest.main()
