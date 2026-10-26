from __future__ import annotations

import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from athlete_growth.adapters.storage import profile_registry


class ProfileRegistryTests(unittest.TestCase):
    def test_registers_profiles_without_replacing_the_first_default(self) -> None:
        registry_type = getattr(profile_registry, "ProfileRegistry", None)
        self.assertIsNotNone(registry_type, "ProfileRegistry must exist")

        with TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.toml"
            registry = registry_type(config_file)
            registry.register("athlete-001")
            registry.register("athlete-002")

            self.assertEqual(registry.default_profile_id(), "athlete-001")
            self.assertEqual(registry.profile_ids(), ("athlete-001", "athlete-002"))
            self.assertEqual(config_file.stat().st_mode & 0o777, 0o600)
            with config_file.open("rb") as handle:
                document = tomllib.load(handle)
            self.assertEqual(document["schema_version"], 1)
            self.assertEqual(document["default_profile_id"], "athlete-001")
            self.assertEqual(document["profile_ids"], ["athlete-001", "athlete-002"])

    def test_unregister_moves_default_and_can_empty_registry(self) -> None:
        registry_type = getattr(profile_registry, "ProfileRegistry", None)
        self.assertIsNotNone(registry_type)

        with TemporaryDirectory() as directory:
            registry = registry_type(Path(directory) / "config.toml")
            registry.register("athlete-001")
            registry.register("athlete-002")

            registry.unregister("athlete-001")
            self.assertEqual(registry.default_profile_id(), "athlete-002")
            registry.unregister("athlete-002")
            self.assertIsNone(registry.default_profile_id())
            self.assertEqual(registry.profile_ids(), ())


if __name__ == "__main__":
    unittest.main()
