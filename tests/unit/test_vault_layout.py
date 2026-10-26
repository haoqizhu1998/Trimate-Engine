from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from athlete_growth.adapters.storage import vault


class VaultLayoutTests(unittest.TestCase):
    def test_creates_isolated_profile_directories(self) -> None:
        layout_type = getattr(vault, "VaultLayout", None)
        self.assertIsNotNone(layout_type, "VaultLayout must exist")

        with TemporaryDirectory() as directory:
            home = Path(directory) / "athlete-growth-home"
            layout = layout_type(home=home, athlete_id="athlete-001")
            layout.ensure()

            self.assertEqual(layout.profile_root, home / "profiles" / "athlete-001")
            self.assertEqual(layout.database_file, layout.profile_root / "db" / "athlete.sqlite3")
            for path in (
                home / "secrets",
                home / "tokens",
                home / "cache",
                home / "exports",
                home / "logs",
                layout.raw,
                layout.database_dir,
                layout.derived,
                layout.reports,
                layout.backups,
            ):
                self.assertTrue(path.is_dir(), f"missing directory: {path}")

    def test_rejects_athlete_id_that_can_escape_profile_directory(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "athlete-growth-home"

            with self.assertRaisesRegex(ValueError, "athlete ID"):
                vault.VaultLayout(home=home, athlete_id="../escape")


if __name__ == "__main__":
    unittest.main()
