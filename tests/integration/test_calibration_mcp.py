from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from athlete_growth.interfaces.cli import main as cli_main
from athlete_growth.interfaces.mcp import server


class CalibrationMCPTests(unittest.TestCase):
    def test_preview_confirm_and_read_are_exposed_without_vault_argument(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "vault"
            with redirect_stdout(StringIO()):
                self.assertEqual(cli_main.main([
                    "init", "--name", "Test", "--timezone", "UTC", "--data-home", str(home)
                ]), 0)
            today = datetime.now(UTC).date().isoformat()
            preview = server.call_tool("preview_sport_calibration", {
                "sport": "cycling", "value": 210, "tested_on": today,
                "protocol": "field_test",
            }, home)
            self.assertEqual(server.call_tool(
                "get_sport_calibration", {"sport": "cycling"}, home
            )["status"], "missing")
            confirmed = server.call_tool("confirm_sport_calibration", {
                "sport": "cycling", "value": 210, "tested_on": today,
                "protocol": "field_test", "preview_hash": preview["preview_hash"],
            }, home)
            self.assertTrue(confirmed["saved"])
            current = server.call_tool("get_sport_calibration", {"sport": "cycling"}, home)
            self.assertEqual(current["metric"], "ftp_watts")
            self.assertNotIn("vault", str(current))
            with self.assertRaises(ValueError):
                server.call_tool("get_sport_calibration", {
                    "sport": "cycling", "data_home": "/tmp/other",
                }, home)


if __name__ == "__main__":
    unittest.main()
