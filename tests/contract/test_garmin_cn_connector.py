from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from athlete_growth.adapters.garmin_cn import connector
from athlete_growth.ports.data_source import SyncWindow


class FakeGarminGateway:
    def fetch(self, start: date, end: date):
        return [
            {
                "kind": "daily-summary",
                "source_id": "2026-10-26",
                "occurred_at": "2026-10-26T00:00:00+08:00",
                "payload": {"steps": 9000, "resting_hr": 52},
            }
        ]

    def healthcheck(self) -> bool:
        return True


class GarminCnConnectorContractTests(unittest.TestCase):
    def test_is_read_only_and_keeps_cn_credentials_region_isolated(self) -> None:
        connector_type = getattr(connector, "GarminCnConnector", None)
        self.assertIsNotNone(connector_type)

        with TemporaryDirectory() as directory:
            home = Path(directory)
            source = connector_type(home=home, gateway=FakeGarminGateway())
            capabilities = source.capabilities()
            batch = source.sync(SyncWindow(date(2026, 10, 26), date(2026, 10, 26)), None)

            self.assertTrue(capabilities.read_only)
            self.assertFalse(capabilities.supports_write)
            self.assertEqual(source.region, "cn")
            self.assertIn("garmin-cn", source.api_key_file.name)
            self.assertIn("garmin-cn", source.token_cache_file.name)
            self.assertEqual(batch.records[0].source, "garmin-cn")
            self.assertEqual(batch.records[0].payload["steps"], 9000)
            self.assertEqual(batch.next_cursor, "2026-10-26")

    def test_revoke_removes_only_china_region_credentials(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            source = connector.GarminCnConnector(home=home, gateway=FakeGarminGateway())
            source.api_key_file.parent.mkdir(parents=True)
            source.token_cache_file.parent.mkdir(parents=True)
            source.api_key_file.write_text("cn-secret", encoding="utf-8")
            source.token_cache_file.write_text("cn-token", encoding="utf-8")
            global_key = source.api_key_file.parent / "garmin-global.key"
            global_key.write_text("global-secret", encoding="utf-8")

            source.revoke()

            self.assertFalse(source.api_key_file.exists())
            self.assertFalse(source.token_cache_file.exists())
            self.assertEqual(global_key.read_text(encoding="utf-8"), "global-secret")


if __name__ == "__main__":
    unittest.main()
