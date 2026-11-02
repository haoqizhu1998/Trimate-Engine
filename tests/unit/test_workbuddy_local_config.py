from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from athlete_growth.adapters.workbuddy_config import (
    configure_workbuddy,
    unconfigure_workbuddy,
)


class WorkBuddyLocalConfigTests(unittest.TestCase):
    def test_merges_preserves_other_servers_and_removes_only_own_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".workbuddy" / "mcp.json"
            path.parent.mkdir()
            existing = {"mcpServers": {"other": {"url": "https://example.test/mcp"}}}
            path.write_text(json.dumps(existing), encoding="utf-8")
            configure_workbuddy(
                python_executable=Path(temporary) / "venv" / "python",
                data_home=Path(temporary) / "data",
                config_path=path,
            )
            configured = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(configured["mcpServers"]["other"], existing["mcpServers"]["other"])
            own = configured["mcpServers"]["athlete-growth"]
            self.assertEqual(own["args"], ["-m", "athlete_growth.interfaces.mcp.server"])
            self.assertEqual(
                json.loads((path.parent / "mcp.json.athlete-growth.bak").read_text()),
                existing,
            )
            unconfigure_workbuddy(path)
            self.assertEqual(json.loads(path.read_text()), existing)

    def test_invalid_existing_config_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mcp.json"
            path.write_text("{invalid", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "existing file was preserved"):
                configure_workbuddy(
                    python_executable=Path(temporary) / "python",
                    data_home=Path(temporary), config_path=path,
                )
            self.assertEqual(path.read_text(), "{invalid")


if __name__ == "__main__":
    unittest.main()
