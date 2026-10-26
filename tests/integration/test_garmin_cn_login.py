from __future__ import annotations

import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from athlete_growth.interfaces.cli import main as cli_main


class FakeGarminCnConnector:
    latest: FakeGarminCnConnector | None = None

    def __init__(self, *, home: Path) -> None:
        self.home = home
        self.email: str | None = None
        self.password: str | None = None
        self.mfa: str | None = None
        type(self).latest = self

    def authenticate(self, *, email: str, password: str, prompt_mfa):
        self.email = email
        self.password = password
        self.mfa = prompt_mfa()
        return {"display_name": "CN Athlete", "region": "cn"}


class GarminCnLoginCommandTests(unittest.TestCase):
    def test_login_prompts_for_secret_values_and_never_accepts_password_argument(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    cli_main.main(
                        [
                            "init",
                            "--name",
                            "CN Athlete",
                            "--timezone",
                            "Asia/Shanghai",
                            "--data-home",
                            str(home),
                        ]
                    ),
                    0,
                )

            output = StringIO()
            with (
                patch(
                    "athlete_growth.interfaces.cli.commands.GarminCnConnector",
                    FakeGarminCnConnector,
                ),
                patch(
                    "athlete_growth.interfaces.cli.commands.getpass.getpass",
                    side_effect=["account-password", "123456"],
                ),
                redirect_stdout(output),
            ):
                exit_code = cli_main.main(
                    [
                        "garmin-cn-login",
                        "--email",
                        "athlete@example.com",
                        "--data-home",
                        str(home),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                json.loads(output.getvalue()),
                {
                    "authenticated": True,
                    "region": "cn",
                    "display_name": "CN Athlete",
                },
            )
            self.assertEqual(FakeGarminCnConnector.latest.email, "athlete@example.com")
            self.assertEqual(FakeGarminCnConnector.latest.password, "account-password")
            self.assertEqual(FakeGarminCnConnector.latest.mfa, "123456")


if __name__ == "__main__":
    unittest.main()
