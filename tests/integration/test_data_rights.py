from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from athlete_growth.adapters.storage import VaultLayout
from athlete_growth.application import data_rights


class DataRightsServiceTests(unittest.TestCase):
    def test_export_verifies_and_restores_profile_without_secrets(self) -> None:
        service_type = getattr(data_rights, "DataRightsService", None)
        self.assertIsNotNone(service_type)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            layout = VaultLayout(home=home, athlete_id="athlete-001")
            layout.ensure()
            layout.database_file.write_bytes(b"sqlite-placeholder")
            (layout.raw / "sample.json").write_text('{"value": 1}', encoding="utf-8")
            (home / "secrets" / "garmin-cn.key").write_text("secret", encoding="utf-8")
            archive = root / "exports" / "profile.zip"
            service = service_type(home)

            service.export_profile("athlete-001", archive)
            verification = service.verify_archive(archive)
            restored_home = root / "restored"
            restored = service.restore_profile(archive, restored_home)

            self.assertTrue(verification.valid)
            self.assertEqual(verification.athlete_id, "athlete-001")
            self.assertEqual(
                (restored.raw / "sample.json").read_text(encoding="utf-8"),
                '{"value": 1}',
            )
            self.assertEqual(restored.database_file.stat().st_mode & 0o777, 0o600)
            self.assertFalse((restored_home / "secrets" / "garmin-cn.key").exists())

    def test_delete_requires_exact_profile_confirmation(self) -> None:
        service_type = getattr(data_rights, "DataRightsService", None)
        self.assertIsNotNone(service_type)

        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            layout = VaultLayout(home=home, athlete_id="athlete-001")
            layout.ensure()
            service = service_type(home)

            with self.assertRaisesRegex(ValueError, "confirmation"):
                service.delete_profile("athlete-001", confirmation="wrong")
            self.assertTrue(layout.profile_root.exists())
            service.delete_profile("athlete-001", confirmation="athlete-001")
            self.assertFalse(layout.profile_root.exists())


if __name__ == "__main__":
    unittest.main()
