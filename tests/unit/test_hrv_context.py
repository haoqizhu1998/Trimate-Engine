from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from athlete_growth.application.report.hrv import overnight_hrv_context
from athlete_growth.domain.records import CanonicalRecord, EvidenceRef


def _night(day: date, value: float | None, *, source: str = "garmin-cn") -> CanonicalRecord:
    occurred = datetime.combine(day, time.min, tzinfo=UTC)
    data = {"calendar_date": day.isoformat()}
    if value is not None:
        data["last_night_avg_ms"] = value
    else:
        data["weekly_avg_ms"] = 54.0
    return CanonicalRecord.create(
        athlete_id="a", kind="hrv", occurred_at=occurred, data=data,
        evidence=(EvidenceRef(source, day.isoformat(), occurred),),
    )


class OvernightHrvContextTests(unittest.TestCase):
    def test_shows_current_night_and_rolling_comparison_without_readiness_verdict(self) -> None:
        on = date(2026, 10, 28)
        records = [_night(on - timedelta(days=day), 48.0 if day < 7 else 60.0)
                   for day in range(28)]
        context = overnight_hrv_context(records, on=on, timezone=ZoneInfo("UTC"))

        self.assertEqual(context["status"], "trend_available")
        self.assertEqual(context["latest_night_ms"], 48.0)
        self.assertTrue(context["is_current_night"])
        self.assertEqual(context["recent_7d_mean_ms"], 48.0)
        self.assertEqual(context["prior_21d_mean_ms"], 60.0)
        self.assertEqual(context["recent_vs_prior_pct"], -20.0)
        self.assertNotIn("ready", str(context["status"]))

    def test_weekly_only_and_legacy_untyped_values_do_not_create_a_daily_signal(self) -> None:
        on = date(2026, 10, 28)
        legacy = CanonicalRecord.create(
            athlete_id="a", kind="hrv",
            occurred_at=datetime(2026, 10, 27, tzinfo=UTC),
            data={"calendar_date": "2026-10-27", "hrv": 54},
        )
        context = overnight_hrv_context(
            [_night(on, None), legacy], on=on, timezone=ZoneInfo("UTC")
        )
        self.assertEqual(context["status"], "missing")
        self.assertEqual(context["legacy_untyped_record_count"], 1)
        self.assertIsNone(context["latest_night_ms"])
        self.assertIsNone(context["recent_7d_mean_ms"])

    def test_stale_latest_night_and_short_history_remain_explicit(self) -> None:
        on = date(2026, 10, 28)
        context = overnight_hrv_context(
            [_night(on - timedelta(days=2), 51.0)], on=on, timezone=ZoneInfo("UTC")
        )
        self.assertEqual(context["status"], "insufficient_trend_history")
        self.assertEqual(context["latest_night_date"], "2026-10-26")
        self.assertFalse(context["is_current_night"])
        self.assertIsNone(context["recent_vs_prior_pct"])

    def test_does_not_mix_sources_when_comparing_windows(self) -> None:
        on = date(2026, 10, 28)
        records = [_night(on - timedelta(days=day), 45.0, source="garmin-cn")
                   for day in range(7)]
        records.extend(
            _night(on - timedelta(days=day), 80.0, source="coros")
            for day in range(7, 28)
        )
        context = overnight_hrv_context(records, on=on, timezone=ZoneInfo("UTC"))
        self.assertEqual(context["source"], "garmin-cn")
        self.assertIsNone(context["prior_21d_mean_ms"])
        self.assertIsNone(context["recent_vs_prior_pct"])


if __name__ == "__main__":
    unittest.main()
