from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from athlete_growth.application.analyze import service
from athlete_growth.domain.records import CanonicalRecord, EvidenceRef


class TrainingAnalyticsTests(unittest.TestCase):
    def test_calculates_deterministic_load_and_preserves_missing_metrics(self) -> None:
        analytics_type = getattr(service, "TrainingAnalytics", None)
        self.assertIsNotNone(analytics_type)
        as_of = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
        records = []
        for days_ago, load in ((1, 70.0), (3, 50.0), (10, 40.0)):
            occurred = as_of - timedelta(days=days_ago)
            records.append(
                CanonicalRecord.create(
                    athlete_id="athlete-001",
                    kind="activity",
                    occurred_at=occurred,
                    data={"training_load": load, "duration_min": 30.0},
                    evidence=(EvidenceRef("file", str(days_ago), occurred, "c" * 64),),
                )
            )

        first = analytics_type().analyze(records, as_of=as_of)
        second = analytics_type().analyze(records, as_of=as_of)

        self.assertEqual(first, second)
        self.assertEqual(first.metrics["acute_load"], 120.0)
        self.assertEqual(first.metrics["sessions_7d"], 2)
        self.assertIsNone(first.metrics["hr_drift_pct"])
        self.assertIn("heart-rate drift", first.data_gaps)
        self.assertEqual(first.algorithm_version, "training-analytics/1.0")

    def test_builds_personal_recovery_baselines_without_treating_missing_as_zero(self) -> None:
        as_of = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
        records = [
            CanonicalRecord.create(
                athlete_id="athlete-001",
                kind="daily-summary",
                occurred_at=as_of - timedelta(days=day),
                data={"resting_hr": value, "body_battery": 60 + day},
            )
            for day, value in ((1, 50), (2, 52), (3, 54))
        ]
        records.extend(
            [
                CanonicalRecord.create(
                    athlete_id="athlete-001",
                    kind="hrv",
                    occurred_at=as_of - timedelta(days=1),
                    data={"hrv": 62},
                ),
                CanonicalRecord.create(
                    athlete_id="athlete-001",
                    kind="sleep",
                    occurred_at=as_of - timedelta(days=1),
                    data={"sleep_hours": 7.5},
                ),
                CanonicalRecord.create(
                    athlete_id="athlete-001",
                    kind="body-weight",
                    occurred_at=as_of - timedelta(days=1),
                    data={"weight_kg": 70.2},
                ),
            ]
        )

        result = service.TrainingAnalytics().analyze(records, as_of=as_of)

        self.assertEqual(result.metrics["resting_hr_baseline_28d"], 52.0)
        self.assertEqual(result.metrics["hrv_baseline_28d"], 62.0)
        self.assertEqual(result.metrics["sleep_hours_baseline_28d"], 7.5)
        self.assertEqual(result.metrics["weight_kg_latest"], 70.2)
        self.assertNotIn("resting heart rate baseline", result.data_gaps)


if __name__ == "__main__":
    unittest.main()
