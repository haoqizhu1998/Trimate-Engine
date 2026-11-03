from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from athlete_growth.application.performance import EndurancePerformanceService
from athlete_growth.domain.records import CanonicalRecord


class MemoryRecords:
    def __init__(self, records: list[CanonicalRecord]) -> None:
        self.records = records

    def list_current(self, athlete_id: str, *, kind: str | None = None):
        return [
            item
            for item in self.records
            if item.athlete_id == athlete_id and (kind is None or item.kind == kind)
        ]


def activity(sport: str, distance_m: float, duration_min: float, *, athlete: str = "a"):
    return CanonicalRecord.create(
        athlete_id=athlete,
        kind="activity",
        occurred_at=datetime(2026, 10, 26, tzinfo=UTC),
        data={
            "sport": sport,
            "distance_m": distance_m,
            "duration_min": duration_min,
            "laps": [{"distance_m": distance_m / 2, "duration_min": duration_min / 2}],
        },
    )


class EndurancePerformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.running = activity("running", 10000, 50)
        self.cycling = activity("cycling", 40000, 60)
        self.swimming = activity("swimming", 1500, 30)
        self.triathlon = activity("triathlon", 51500, 180)
        self.foreign = activity("running", 5000, 25, athlete="b")
        self.service = EndurancePerformanceService(
            "a",
            MemoryRecords(
                [self.running, self.cycling, self.swimming, self.triathlon, self.foreign]
            ),
            now=datetime(2026, 10, 27, tzinfo=UTC),
        )

    def test_sport_units_and_triathlon_guard(self) -> None:
        self.assertEqual(
            self.service.analyze_session(self.running.record_id)["summary"],
            {"pace_min_per_km": 5.0},
        )
        self.assertEqual(
            self.service.analyze_session(self.cycling.record_id)["summary"],
            {"speed_km_per_hour": 40.0},
        )
        self.assertEqual(
            self.service.analyze_session(self.swimming.record_id)["summary"],
            {"pace_min_per_100m": 2.0},
        )
        triathlon = self.service.analyze_session(self.triathlon.record_id)
        self.assertEqual(triathlon["summary"], {})
        self.assertIn("segment disciplines", " ".join(triathlon["data_gaps"]))

    def test_athlete_ownership_and_insufficient_baseline(self) -> None:
        with self.assertRaisesRegex(ValueError, "current activity record not found"):
            self.service.analyze_session(self.foreign.record_id)
        baseline = self.service.get_sport_baseline("running")
        self.assertEqual(baseline["sample_count"], 1)
        self.assertEqual(baseline["status"], "insufficient")
        self.assertNotIn(self.foreign.record_id, baseline["evidence_ids"])

    def test_training_load_context_is_sport_scoped_and_descriptive(self) -> None:
        context = self.service.get_training_load_context(sport="running")
        self.assertEqual(len(context["sports"]), 1)
        self.assertEqual(context["sports"][0]["sessions_7d"], 1)
        self.assertIsNone(context["sports"][0]["load_ratio"])
        self.assertNotIn(self.foreign.record_id, str(context))

    def test_similar_sessions_use_only_prior_comparable_quality_checked_history(self) -> None:
        current = CanonicalRecord.create(
            athlete_id="a", kind="activity",
            occurred_at=datetime(2026, 10, 26, tzinfo=UTC),
            data={"sport": "running", "duration_min": 50, "distance_m": 10000},
        )
        peers = [
            CanonicalRecord.create(
                athlete_id="a", kind="activity",
                occurred_at=current.occurred_at - timedelta(days=offset),
                data={"sport": "running", "duration_min": minutes, "distance_m": 10000},
            )
            for offset, minutes in ((7, 49), (14, 50), (21, 51))
        ]
        excluded = CanonicalRecord.create(
            athlete_id="a", kind="activity",
            occurred_at=current.occurred_at - timedelta(days=3),
            data={"sport": "running", "duration_min": 90, "distance_m": 10000},
        )
        future = CanonicalRecord.create(
            athlete_id="a", kind="activity",
            occurred_at=current.occurred_at + timedelta(days=1),
            data={"sport": "running", "duration_min": 40, "distance_m": 10000},
        )
        result = EndurancePerformanceService(
            "a", MemoryRecords([current, *peers, excluded, future]),
            now=datetime(2026, 10, 27, tzinfo=UTC),
        ).analyze_session(current.record_id)["similar_sessions"]
        self.assertEqual(result["status"], "descriptive")
        self.assertEqual(result["sample_count"], 3)
        self.assertEqual(result["median"], 5.0)
        self.assertEqual(result["current"], 5.0)
        self.assertEqual(result["difference_percent"], 0.0)
        self.assertNotIn(excluded.record_id, result["evidence_ids"])
        self.assertNotIn(future.record_id, result["evidence_ids"])
        self.assertIn("not controlled", " ".join(result["limitations"]))

    def test_lap_pacing_pattern_requires_comparable_complete_laps(self) -> None:
        record = CanonicalRecord.create(
            athlete_id="a", kind="activity",
            occurred_at=datetime(2026, 10, 26, tzinfo=UTC),
            data={
                "sport": "running", "duration_min": 22, "distance_m": 4000,
                "laps": [
                    {"distance_m": 1000, "duration_min": minutes}
                    for minutes in (5, 5, 6, 6)
                ],
            },
        )
        pattern = EndurancePerformanceService(
            "a", MemoryRecords([record])
        ).analyze_session(record.record_id)["lap_pacing_pattern"]
        self.assertEqual(pattern["status"], "descriptive")
        self.assertEqual(pattern["first_half_median"], 5)
        self.assertEqual(pattern["second_half_median"], 6)
        self.assertEqual(pattern["difference_percent"], 20)

        uneven = CanonicalRecord.create(
            athlete_id="a", kind="activity",
            occurred_at=datetime(2026, 10, 26, tzinfo=UTC),
            data={
                "sport": "running", "duration_min": 23, "distance_m": 4500,
                "laps": [
                    {"distance_m": distance, "duration_min": minutes}
                    for distance, minutes in ((1000, 5), (1000, 5), (1000, 6), (1500, 7))
                ],
            },
        )
        refused = EndurancePerformanceService(
            "a", MemoryRecords([uneven])
        ).analyze_session(uneven.record_id)["lap_pacing_pattern"]
        self.assertEqual(refused["status"], "insufficient")
        self.assertIn("distances differ", refused["reason"])

    def test_explicit_plan_actual_comparison_does_not_claim_completion(self) -> None:
        plan = CanonicalRecord.create(
            athlete_id="a",
            kind="training-plan",
            occurred_at=datetime(2026, 10, 20, tzinfo=UTC),
            data={
                "plan_id": "plan-1",
                "sessions": [
                    {
                        "session_id": "session-1",
                        "session_date": "2026-10-20",
                        "sport": "running",
                        "duration_min": 40,
                        "intensity": "easy",
                    }
                ],
            },
        )
        self.service.records.records.append(plan)
        result = self.service.compare_plan_actual(
            "plan-1", "session-1", self.running.record_id
        )
        self.assertEqual(result["duration_delta_min"], 10)
        self.assertTrue(result["same_sport"])
        self.assertNotIn("completed", result)


if __name__ == "__main__":
    unittest.main()
