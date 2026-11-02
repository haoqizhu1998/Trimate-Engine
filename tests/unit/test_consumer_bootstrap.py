from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).parents[2] / "packaging" / "common" / "bootstrap-local.py"
SPEC = importlib.util.spec_from_file_location("consumer_bootstrap", SOURCE)
assert SPEC and SPEC.loader
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class ConsumerBootstrapTests(unittest.TestCase):
    def test_windows_china_timezone_uses_local_day(self) -> None:
        with patch.dict("os.environ", {"TZ": ""}), patch.object(
            bootstrap.platform, "system", return_value="Windows"
        ), patch.object(
            bootstrap,
            "_run",
            return_value=subprocess.CompletedProcess([], 0, "China Standard Time\n"),
        ):
            self.assertEqual(bootstrap._local_timezone(), "Asia/Shanghai")

    def test_existing_unreadable_vault_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = root / "cli"
            cli.write_text("#!/bin/sh\nexit 1\n")
            cli.chmod(0o700)
            home = root / "private"
            home.mkdir()
            marker = home / "keep.txt"
            marker.write_text("keep")
            with self.assertRaisesRegex(RuntimeError, "已保留原数据"):
                bootstrap.ensure_profile(cli, home)
            self.assertEqual(marker.read_text(), "keep")

    def test_codex_config_preserves_other_entries_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            original = '[mcp_servers.other]\ncommand = "other-server"\n'
            config.write_text(original, encoding="utf-8")
            python = root / "venv" / "python"
            home = root / "data"
            self.assertEqual(bootstrap.configure_codex(python, home, config), "configured")
            self.assertEqual(bootstrap.configure_codex(python, home, config), "already_configured")
            self.assertEqual(config.read_text().count("[mcp_servers.athlete_growth]"), 1)
            self.assertEqual((root / "config.toml.trimate.bak").read_text(), original)
            self.assertIn('command = "other-server"', config.read_text())

    def test_codex_conflict_and_invalid_file_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            for content in ('[mcp_servers.athlete_growth]\ncommand = "different"\n', "invalid = ["):
                path.write_text(content)
                with self.assertRaises(RuntimeError):
                    bootstrap.configure_codex(Path("/python"), Path("/data"), path)
                self.assertEqual(path.read_text(), content)


if __name__ == "__main__":
    unittest.main()
