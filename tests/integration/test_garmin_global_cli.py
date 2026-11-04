from __future__ import annotations

import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from athlete_growth.interfaces.cli import main as cli_main


class FakeGlobalConnector:
    key: str | None = None

    def __init__(self, **_kwargs: object) -> None:
        pass

    def connect(self, key: str) -> dict[str, object]:
        type(self).key = key
        return {"connected": True, "source": "garmin-global", "region": "global"}


class GarminGlobalCliTests(unittest.TestCase):
    def test_hidden_key_connect_enables_automatic_source_policy(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "vault"
            with redirect_stdout(StringIO()):
                self.assertEqual(cli_main.main([
                    "init", "--name", "Synthetic Athlete", "--timezone", "Asia/Shanghai",
                    "--data-home", str(home),
                ]), 0)
            output = StringIO()
            with (
                patch(
                    "athlete_growth.interfaces.cli.commands.GarminGlobalConnector",
                    FakeGlobalConnector,
                ),
                patch(
                    "athlete_growth.interfaces.cli.commands.getpass.getpass",
                    return_value="synthetic-key",
                ),
                patch("athlete_growth.interfaces.cli.commands.sys.stdin.isatty", return_value=True),
                redirect_stdout(output),
            ):
                self.assertEqual(cli_main.main([
                    "garmin-global-connect", "--data-home", str(home),
                ]), 0)
            result = json.loads(output.getvalue())
            self.assertTrue(result["connected"])
            self.assertEqual(result["auto_sync"]["policy"]["source"], "garmin-global")
            self.assertEqual(FakeGlobalConnector.key, "synthetic-key")
            self.assertNotIn("synthetic-key", output.getvalue())

            revoked = StringIO()
            with (
                patch(
                    "athlete_growth.interfaces.cli.commands.GarminGlobalConnector",
                    FakeGlobalConnector,
                ),
                patch.object(FakeGlobalConnector, "revoke", return_value=None, create=True),
                redirect_stdout(revoked),
            ):
                self.assertEqual(cli_main.main([
                    "garmin-global-revoke", "--data-home", str(home),
                ]), 0)
            self.assertFalse(json.loads(revoked.getvalue())["auto_sync_enabled"])
            from athlete_growth.interfaces.host.tools import automation_status

            policy = next(
                item for item in automation_status(home)["policies"]
                if item["source"] == "garmin-global"
            )
            self.assertFalse(policy["enabled"])


if __name__ == "__main__":
    unittest.main()
