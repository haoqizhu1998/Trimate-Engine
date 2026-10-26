"""User feedback and personal response learning."""

from __future__ import annotations

from datetime import datetime
from math import sqrt

from athlete_growth.adapters.storage.sqlite_records import SQLiteRecordStore
from athlete_growth.domain.growth import ResponseInsight
from athlete_growth.domain.records import CanonicalRecord


class PersonalResponseLearner:
    def analyze(
        self,
        *,
        stimulus_name: str,
        outcome_name: str,
        samples: list[tuple[float, float]],
    ) -> ResponseInsight:
        if len(samples) < 3:
            raise ValueError("at least three samples are required")
        xs = [sample[0] for sample in samples]
        ys = [sample[1] for sample in samples]
        x_mean = sum(xs) / len(xs)
        y_mean = sum(ys) / len(ys)
        numerator = sum((x - x_mean) * (y - y_mean) for x, y in samples)
        denominator = sqrt(sum((x - x_mean) ** 2 for x in xs) * sum((y - y_mean) ** 2 for y in ys))
        correlation = numerator / denominator if denominator else 0.0
        statement = (
            f"Observed association between {stimulus_name} and {outcome_name} "
            f"across {len(samples)} samples (r={correlation:.2f}); "
            "this does not establish causation."
        )
        return ResponseInsight(
            stimulus_name,
            outcome_name,
            round(correlation, 3),
            len(samples),
            statement,
        )

    def record_feedback(
        self,
        *,
        athlete_id: str,
        recommendation_id: str,
        accepted: bool,
        outcome: str | None,
        recorded_at: datetime,
        store: SQLiteRecordStore,
    ) -> CanonicalRecord:
        record = CanonicalRecord.create(
            athlete_id=athlete_id,
            kind="recommendation-feedback",
            occurred_at=recorded_at,
            data={
                "recommendation_id": recommendation_id,
                "accepted": accepted,
                "outcome": outcome,
            },
        )
        store.save(record)
        return record
