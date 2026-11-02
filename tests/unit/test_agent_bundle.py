from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from athlete_growth.experience import bundle


class AgentBundleTests(unittest.TestCase):
    def test_exported_bundle_uses_existing_tool_contract_and_is_portable(self) -> None:
        manifest = bundle.load_manifest()
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "out" / "generic" / "stale.json"
            old.parent.mkdir(parents=True)
            old.write_text("{}", encoding="utf-8")
            output = bundle.build_bundle(Path(tmp) / "out")
            metadata = json.loads((output / "bundle.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["capability_version"], "1.18")
            self.assertEqual(
                len(metadata["skills"]), len(manifest["skills"]),
            )
            codex = output / "codex"
            entry = (codex / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("每轮用户对话", entry)
            self.assertIn("项目级常驻 Agent", entry)
            self.assertFalse((codex / "athlete-training-partner").exists())
            self.assertEqual(
                json.loads((codex / "experience" / "manifest.json").read_text(encoding="utf-8")),
                manifest,
            )
            self.assertTrue((output / "generic" / "agent.md").is_file())
            self.assertFalse(old.exists())
            generic_text = (output / "generic" / "agent.md").read_text(encoding="utf-8")
            self.assertTrue((codex / "experience" / "host-contract.md").is_file())
            docs = codex / "experience" / "docs"
            self.assertTrue((docs / "endurance-training-methodology.md").is_file())
            for skill in manifest["skills"]:
                name = skill["id"]
                self.assertTrue(
                    (codex / "experience" / "skills" / name / "SKILL.md").is_file()
                )
                self.assertTrue((output / "generic" / "skills" / name / "SKILL.md").is_file())
                self.assertIn(f"(skills/{name}/SKILL.md)", generic_text)
            skills = codex / "experience" / "skills"
            self.assertTrue(
                (skills / "body-assessment" / "references" / "output-template.md").is_file()
            )
            self.assertTrue(
                (skills / "training-plan" / "references" / "periodization.md").is_file()
            )

    def test_all_skills_are_product_owned_and_active(self) -> None:
        manifest = bundle.load_manifest()
        source = manifest["skills"]
        self.assertEqual(len(source), 17)
        self.assertTrue(all("origin" not in skill for skill in source))
        self.assertEqual(
            sum(skill.get("availability") == "source_only" for skill in source), 0
        )

    def test_missing_tool_fails_before_bundle_export(self) -> None:
        real = bundle.EXPERIENCE
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            (source / "skills" / "example").mkdir(parents=True)
            (source / "agent.md").write_text("agent", encoding="utf-8")
            (source / "skills" / "example" / "SKILL.md").write_text(
                "---\nname: example\ndescription: example\n---\n\n# Example\n",
                encoding="utf-8",
            )
            manifest = {
                "schema_version": 1,
                "agent": {
                    "id": "athlete-training-partner",
                    "version": "0.1.0",
                    "source": "agent.md",
                    "minimum_capability_version": "1.18",
                },
                "skills": [
                    {
                        "id": "example",
                        "version": "0.1.0",
                        "source": "skills/example/SKILL.md",
                        "required_tools": ["nonexistent_tool"],
                    }
                ],
            }
            (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with (
                patch.object(bundle, "EXPERIENCE", source),
                self.assertRaisesRegex(ValueError, "unavailable tools"),
            ):
                bundle.build_bundle(source / "output")
        self.assertEqual(bundle.EXPERIENCE, real)


if __name__ == "__main__":
    unittest.main()
