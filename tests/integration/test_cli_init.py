from __future__ import annotations

import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from athlete_growth.adapters.storage import (
    ProfileRegistry,
    SQLiteAthleteProfileRepository,
    VaultLayout,
)
from athlete_growth.interfaces.cli import main as cli_main


class InitCommandTests(unittest.TestCase):
    def test_initializes_a_runnable_local_profile(self) -> None:
        main = getattr(cli_main, "main", None)
        self.assertIsNotNone(main, "CLI main must exist")

        with TemporaryDirectory() as directory:
            home = Path(directory) / "athlete-growth-home"
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "init",
                        "--name",
                        "Demo Athlete",
                        "--timezone",
                        "Asia/Shanghai",
                        "--units",
                        "metric",
                        "--data-home",
                        str(home),
                    ]
                )

            self.assertEqual(exit_code, 0)
            athlete_id = ProfileRegistry(home / "config.toml").default_profile_id()
            self.assertIsNotNone(athlete_id)
            layout = VaultLayout(home=home, athlete_id=athlete_id)
            stored_profile = SQLiteAthleteProfileRepository(layout.database_file).get_by_id(
                athlete_id
            )
            self.assertIsNotNone(stored_profile)
            self.assertEqual(stored_profile.display_name, "Demo Athlete")
            self.assertEqual(stored_profile.timezone_name, "Asia/Shanghai")
            self.assertIn(athlete_id, output.getvalue())
            self.assertIn(str(layout.profile_root), output.getvalue())

    def test_status_reads_the_default_profile(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "athlete-growth-home"
            with redirect_stdout(StringIO()):
                cli_main.main(
                    [
                        "init",
                        "--name",
                        "Demo Athlete",
                        "--timezone",
                        "Asia/Shanghai",
                        "--data-home",
                        str(home),
                    ]
                )

            output = StringIO()
            with redirect_stdout(output):
                exit_code = cli_main.main(["status", "--data-home", str(home)])

            athlete_id = ProfileRegistry(home / "config.toml").default_profile_id()
            self.assertEqual(exit_code, 0)
            self.assertIn(athlete_id, output.getvalue())
            self.assertIn("Demo Athlete", output.getvalue())
            self.assertIn("Asia/Shanghai", output.getvalue())

    def test_status_reports_an_uninitialized_data_home(self) -> None:
        with TemporaryDirectory() as directory:
            error = StringIO()
            with redirect_stderr(error):
                exit_code = cli_main.main(["status", "--data-home", str(Path(directory) / "empty")])

            self.assertEqual(exit_code, 1)
            self.assertIn("not initialized", error.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
