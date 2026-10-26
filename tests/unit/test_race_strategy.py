from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from athlete_growth.adapters.races import file_connector
from athlete_growth.application.race import service


class RaceStrategyTests(unittest.TestCase):
    def test_import_preserves_source_and_strategy_exports_offline(self) -> None:
        connector_type = getattr(file_connector, "RaceFileConnector", None)
        strategy_type = getattr(service, "RaceStrategyService", None)
        self.assertIsNotNone(connector_type)
        self.assertIsNotNone(strategy_type)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            source_file = root / "race.json"
            source_file.write_text(
                json.dumps(
                    {
                        "event_id": "race-1",
                        "name": "Demo Half Marathon",
                        "event_date": "2027-01-10",
                        "distance_km": 21.0975,
                        "elevation_gain_m": 120,
                        "location": "Demo City",
                        "source_url": "https://example.test/race",
                        "source_updated_at": "2026-10-25T00:00:00+00:00",
                        "license": "user-provided",
                    }
                ),
                encoding="utf-8",
            )
            event = connector_type().read(source_file)[0]
            strategy = strategy_type().build(
                event=event,
                target_finish_min=110,
                forecast=None,
                travel_context={"timezone_shift_hours": 2},
            )
            output = strategy_type().export_markdown(strategy, root / "strategy.md")

            self.assertEqual(event.source_url, "https://example.test/race")
            self.assertIn("weather forecast unavailable", strategy.data_gaps)
            self.assertTrue(output.is_file())
            self.assertIn("Demo Half Marathon", output.read_text(encoding="utf-8"))
            self.assertIn("user-provided", output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
