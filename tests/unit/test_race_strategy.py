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

    def test_short_running_race_does_not_get_long_race_fueling_target(self) -> None:
        from datetime import UTC, date, datetime

        from athlete_growth.domain.race import RaceEvent

        event = RaceEvent(
            "10k",
            "Synthetic 10K",
            date(2027, 1, 10),
            10,
            0,
            "Demo City",
            "unverified:user-input",
            datetime.now(UTC),
            "user-provided/unverified",
        )
        strategy = service.RaceStrategyService().build(
            event=event, target_finish_min=55, forecast=None
        )
        self.assertTrue(any("short races" in item for item in strategy.fueling))
        self.assertFalse(any("60–90" in item for item in strategy.fueling))


if __name__ == "__main__":
    unittest.main()
