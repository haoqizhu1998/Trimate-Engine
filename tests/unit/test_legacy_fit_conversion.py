from __future__ import annotations

import importlib.util
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "convert_fit_to_tcx.py"
SPEC = importlib.util.spec_from_file_location("legacy_fit_converter", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
converter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(converter)


class LegacyFitConversionTests(unittest.TestCase):
    def test_multisport_collapse_preserves_total_distance_time_and_hr(self) -> None:
        start = datetime(2026, 10, 26, tzinfo=UTC)
        sessions = [
            {
                "sport": "running",
                "start_time": start,
                "total_timer_time": 600,
                "total_distance": 2_000,
                "total_calories": 150,
                "avg_heart_rate": 140,
                "max_heart_rate": 160,
            },
            {
                "sport": "cycling",
                "start_time": start + timedelta(minutes=10),
                "total_timer_time": 1_200,
                "total_distance": 8_000,
                "total_calories": 250,
                "avg_heart_rate": 130,
                "max_heart_rate": 155,
            },
        ]
        records = [{"timestamp": start + timedelta(minutes=30)}]

        merged = converter.collapse_multisport_sessions(sessions, records)[0]

        self.assertEqual(merged["total_distance"], 10_000)
        self.assertEqual(merged["total_timer_time"], 1_800)
        self.assertEqual(merged["total_calories"], 400)
        self.assertAlmostEqual(merged["avg_heart_rate"], 133.333333, places=5)
        self.assertEqual(merged["max_heart_rate"], 160)

    def test_session_record_selection_uses_elapsed_time_when_end_is_invalid(self) -> None:
        start = datetime(2026, 10, 26, tzinfo=UTC)
        records = [
            {"timestamp": start - timedelta(seconds=1)},
            {"timestamp": start + timedelta(seconds=30)},
            {"timestamp": start + timedelta(seconds=61)},
        ]
        session = {
            "start_time": start,
            "timestamp": start,
            "total_elapsed_time": 60,
        }

        selected = converter.session_records(records, session)

        self.assertEqual(selected, [records[1]])


if __name__ == "__main__":
    unittest.main()
