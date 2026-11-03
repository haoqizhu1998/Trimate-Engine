"""Sport-aware descriptive analysis; never infer unobserved thresholds or race results."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, time, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from athlete_growth.application.analyze.load import comparable_load_window
from athlete_growth.domain.records import CanonicalRecord, JsonValue
from athlete_growth.ports.repositories import CanonicalRecordRepository

SPORTS = ("running", "cycling", "swimming", "triathlon")
_ALIASES = {
    "run": "running",
    "running": "running",
    "trail_running": "running",
    "treadmill_running": "running",
    "road_running": "running",
    "cycling": "cycling",
    "biking": "cycling",
    "road_biking": "cycling",
    "road_cycling": "cycling",
    "indoor_cycling": "cycling",
    "virtual_ride": "cycling",
    "mountain_biking": "cycling",
    "swim": "swimming",
    "swimming": "swimming",
    "lap_swimming": "swimming",
    "open_water_swimming": "swimming",
    "triathlon": "triathlon",
    "multisport": "triathlon",
}
_SAFE_METRICS = (
    "name",
    "sport",
    "duration_min",
    "distance_m",
    "avg_hr",
    "max_hr",
    "avg_speed_mps",
    "avg_power_w",
    "avg_cadence_rpm",
    "elevation_gain_m",
    "training_load",
)
_MAX_LAPS = 200


def normalize_sport(raw: object) -> str:
    if not isinstance(raw, str):
        return "other"
    return _ALIASES.get(raw.strip().lower().replace(" ", "_"), "other")


def _number(data: dict[str, JsonValue], key: str) -> float | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def _performance_metrics(
    sport: str, duration: float | None, distance: float | None
) -> dict[str, float]:
    if duration is None or distance is None or duration <= 0 or distance <= 0:
        return {}
    if sport == "running":
        return {"pace_min_per_km": round(duration / (distance / 1000), 3)}
    if sport == "cycling":
        return {"speed_km_per_hour": round((distance / 1000) / (duration / 60), 3)}
    if sport == "swimming":
        return {"pace_min_per_100m": round(duration / (distance / 100), 3)}
    return {}


def _safe_activity_metrics(data: dict[str, JsonValue]) -> dict[str, JsonValue]:
    metrics: dict[str, JsonValue] = {}
    for key in _SAFE_METRICS:
        if key in {"name", "sport"}:
            value = data.get(key)
            if isinstance(value, str):
                metrics[key] = value[:200]
        else:
            number = _number(data, key)
            if number is not None:
                metrics[key] = number
    return metrics


class EndurancePerformanceService:
    """Read only current, athlete-owned canonical records and report evidence gaps."""

    def __init__(
        self,
        athlete_id: str,
        records: CanonicalRecordRepository,
        *,
        timezone_name: str = "UTC",
        now: datetime | None = None,
    ) -> None:
        self.athlete_id = athlete_id
        self.records = records
        self.timezone = ZoneInfo(timezone_name)
        self.now = now or datetime.now(UTC)

    def _current(self, kind: str, record_id: str) -> CanonicalRecord:
        selected = next(
            (
                record
                for record in self.records.list_current(self.athlete_id, kind=kind)
                if record.record_id == record_id
            ),
            None,
        )
        if selected is None:
            raise ValueError(f"current {kind} record not found")
        return selected

    def get_activity_detail(self, record_id: str) -> dict[str, object]:
        record = self._current("activity", record_id)
        sport = normalize_sport(record.data.get("sport"))
        metrics = _safe_activity_metrics(record.data)
        laps = self._laps(record)
        gaps = self._gaps(sport, record, laps)
        return {
            "record_id": record.record_id,
            "occurred_at": record.occurred_at.isoformat(),
            "sport": sport,
            "source_sport": metrics.get("sport"),
            "metrics": metrics,
            "laps": laps,
            "laps_truncated": self._raw_lap_count(record) > _MAX_LAPS,
            "detail_sync": {
                "status": record.data.get("detail_status", "summary_only"),
                "missing_sections": record.data.get("detail_missing_sections", []),
                "recorded_sample_count": record.data.get("recorded_sample_count", 0),
                "original_file_cached": isinstance(
                    record.data.get("original_fit_zip_sha256"), str
                ),
            },
            "quality_flags": list(record.quality_flags),
            "source_count": len(record.evidence),
            "data_gaps": gaps,
        }

    def analyze_session(self, record_id: str) -> dict[str, object]:
        record = self._current("activity", record_id)
        detail = self.get_activity_detail(record_id)
        metrics = detail["metrics"]
        assert isinstance(metrics, dict)
        sport = str(detail["sport"])
        duration = _number(metrics, "duration_min")
        distance = _number(metrics, "distance_m")
        summary = _performance_metrics(sport, duration, distance)
        raw_laps = detail["laps"]
        assert isinstance(raw_laps, list)
        lap_results = [
            {
                "index": lap["index"],
                "duration_min": lap.get("duration_min"),
                "distance_m": lap.get("distance_m"),
                **({"avg_hr": lap["avg_hr"]} if "avg_hr" in lap else {}),
                **({"avg_power_w": lap["avg_power_w"]} if "avg_power_w" in lap else {}),
                **_performance_metrics(
                    sport,
                    _number(lap, "duration_min"),
                    _number(lap, "distance_m"),
                ),
            }
            for lap in raw_laps
        ]
        if sport == "triathlon":
            raw_gaps = detail["data_gaps"]
            assert isinstance(raw_gaps, list)
            raw_gaps.append(
                "segment disciplines and transitions are required; no overall pace is calculated"
            )
        detail_sync = detail["detail_sync"]
        assert isinstance(detail_sync, dict)
        if detail_sync.get("recorded_sample_count", 0):
            raw_gaps = detail["data_gaps"]
            assert isinstance(raw_gaps, list)
            raw_gaps.append("recorded samples are cached but not yet analyzed point by point")
        return {
            "record_id": record_id,
            "sport": sport,
            "summary": summary,
            "lap_splits": lap_results,
            "similar_sessions": self._similar_sessions(record, sport, summary),
            "metrics": metrics,
            "quality_flags": detail["quality_flags"],
            "data_gaps": detail["data_gaps"],
            "evidence": {"record_id": record_id, "source_count": detail["source_count"]},
            "interpretation": "descriptive only; no threshold, race prediction or causal claim",
        }

    def _similar_sessions(
        self,
        record: CanonicalRecord,
        sport: str,
        current_metrics: dict[str, float],
    ) -> dict[str, object]:
        """Describe prior sessions with similar exposure, without claiming fitness change."""
        metric = {
            "running": "pace_min_per_km",
            "cycling": "speed_km_per_hour",
            "swimming": "pace_min_per_100m",
        }.get(sport)
        duration = _number(record.data, "duration_min")
        distance = _number(record.data, "distance_m")
        empty: dict[str, object] = {
            "status": "insufficient",
            "sample_count": 0,
            "metric": metric,
            "current": current_metrics.get(metric) if metric else None,
            "median": None,
            "difference_percent": None,
            "evidence_ids": [],
            "limitations": ["effort, terrain and weather are not controlled"],
        }
        if metric is None or duration is None or distance is None or not current_metrics:
            return {**empty, "limitations": ["comparable sport, duration or distance unavailable"]}
        if record.quality_flags:
            return {**empty, "limitations": ["current activity has quality flags"]}
        peers: list[tuple[CanonicalRecord, float]] = []
        for candidate in self.records.list_current(self.athlete_id, kind="activity"):
            if (
                candidate.record_id == record.record_id
                or candidate.quality_flags
                or not record.occurred_at - timedelta(days=180)
                <= candidate.occurred_at < record.occurred_at
                or normalize_sport(candidate.data.get("sport")) != sport
                or candidate.data.get("sport") != record.data.get("sport")
            ):
                continue
            candidate_duration = _number(candidate.data, "duration_min")
            candidate_distance = _number(candidate.data, "distance_m")
            if (
                candidate_duration is None
                or candidate_distance is None
                or not 0.8 <= candidate_duration / duration <= 1.2
                or not 0.8 <= candidate_distance / distance <= 1.2
            ):
                continue
            current_type = record.data.get("session_type")
            candidate_type = candidate.data.get("session_type")
            if current_type and candidate_type and current_type != candidate_type:
                continue
            value = _performance_metrics(sport, candidate_duration, candidate_distance)
            if metric in value:
                peers.append((candidate, value[metric]))
        peers.sort(key=lambda item: item[0].occurred_at, reverse=True)
        peers = peers[:20]
        if len(peers) < 3:
            return {
                **empty,
                "sample_count": len(peers),
                "evidence_ids": [item.record_id for item, _ in peers],
                "limitations": ["fewer than three comparable prior sessions"],
            }
        return {
            "status": "descriptive",
            "sample_count": len(peers),
            "metric": metric,
            "current": current_metrics[metric],
            "median": round(median(value for _, value in peers), 3),
            "difference_percent": round(
                (current_metrics[metric] / median(value for _, value in peers) - 1) * 100,
                2,
            ),
            "evidence_ids": [item.record_id for item, _ in peers],
            "selection": (
                "up to 20 most recent, same source sport; prior 180 days; "
                "duration and distance within 20%"
            ),
            "limitations": [
                "effort, terrain and weather are not controlled",
                "difference is current metric minus reference median; for pace, lower is faster",
                "this is not a threshold, fitness trend or race prediction",
            ],
        }

    def get_sport_baseline(self, sport: str, *, days: int = 90) -> dict[str, object]:
        if sport not in SPORTS or not 7 <= days <= 365:
            raise ValueError("sport or baseline window is invalid")
        start = self.now - timedelta(days=days)
        activities = [
            record
            for record in self.records.list_current(self.athlete_id, kind="activity")
            if start <= record.occurred_at <= self.now
            and normalize_sport(record.data.get("sport")) == sport
        ]
        activities.sort(key=lambda item: item.occurred_at, reverse=True)
        valid = [
            record
            for record in activities
            if not record.quality_flags
            and (_number(record.data, "duration_min") or 0) > 0
            and (_number(record.data, "distance_m") or 0) > 0
        ]
        key = {
            "running": "pace_min_per_km",
            "cycling": "speed_km_per_hour",
            "swimming": "pace_min_per_100m",
        }.get(sport)
        values = [
            _performance_metrics(
                sport,
                _number(record.data, "duration_min"),
                _number(record.data, "distance_m"),
            )
            for record in valid
        ]
        descriptive = {
            "median_duration_min": round(
                median(_number(item.data, "duration_min") or 0 for item in valid), 2
            ),
            "median_distance_m": round(
                median(_number(item.data, "distance_m") or 0 for item in valid), 2
            ),
        } if valid else {}
        if key and values:
            descriptive[key] = round(median(item[key] for item in values), 3)
        gaps = []
        if len(valid) < 3:
            gaps.append("fewer than three quality-checked sessions; baseline is insufficient")
        if len(valid) < len(activities):
            gaps.append("some sessions lacked distance/duration or had quality flags")
        if sport == "triathlon":
            gaps.append("triathlon legs are not combined into a single pace or speed baseline")
        return {
            "sport": sport,
            "window_days": days,
            "sample_count": len(valid),
            "total_sessions": len(activities),
            "status": "descriptive" if len(valid) >= 3 else "insufficient",
            "descriptive_metrics": descriptive,
            "evidence_ids": [item.record_id for item in valid[:50]],
            "latest_activity_at": activities[0].occurred_at.isoformat() if activities else None,
            "data_gaps": gaps,
            "interpretation": "observed history only; not a threshold or performance prediction",
        }

    def get_training_load_context(
        self, *, sport: str | None = None, end_date: date | None = None
    ) -> dict[str, object]:
        if sport is not None and sport not in SPORTS:
            raise ValueError("training load sport is invalid")
        today = self.now.astimezone(self.timezone).date()
        end_day = end_date or today
        if end_day > today:
            raise ValueError("training load end date is in the future")
        as_of = datetime.combine(end_day, time.max, tzinfo=self.timezone).astimezone(UTC)
        start_day = end_day - timedelta(days=27)
        acute_day = end_day - timedelta(days=6)
        start = datetime.combine(start_day, time.min, tzinfo=self.timezone).astimezone(UTC)
        acute_start = datetime.combine(acute_day, time.min, tzinfo=self.timezone).astimezone(UTC)
        activities = [
            record for record in self.records.list_current(self.athlete_id, kind="activity")
            if start <= record.occurred_at <= min(as_of, self.now)
        ]
        selected_sports = (sport,) if sport is not None else SPORTS
        rows = []
        for selected in selected_sports:
            history = [
                record for record in activities
                if normalize_sport(record.data.get("sport")) == selected
            ]
            if not history and sport is None:
                continue
            recent = [record for record in history if record.occurred_at >= acute_start]
            load = comparable_load_window(history, as_of=as_of, acute_start=acute_start)
            rows.append({
                "sport": selected,
                "sessions_7d": len(recent),
                "minutes_7d": round(sum(_number(r.data, "duration_min") or 0 for r in recent), 2),
                "distance_m_7d": round(sum(_number(r.data, "distance_m") or 0 for r in recent), 2),
                "sessions_28d": len(history),
                "load_method": load.method,
                "load_7d": load.acute,
                "load_28d_weekly_average": load.chronic_weekly,
                "load_ratio": load.ratio,
                "comparable_load_sessions": load.comparable_count,
                "data_gaps": list(load.gaps),
            })
        return {
            "window": {"start": start_day.isoformat(), "end": end_day.isoformat()},
            "sports": rows,
            "interpretation": (
                "Separate sports and score methods; a load ratio is descriptive context "
                "only and never an injury prediction or clearance to add training."
            ),
        }

    def compare_plan_actual(
        self, plan_id: str, session_id: str, activity_id: str
    ) -> dict[str, object]:
        plan = next(
            (
                item
                for item in self.records.list_current(self.athlete_id, kind="training-plan")
                if str(item.data.get("plan_id")) == plan_id
            ),
            None,
        )
        if plan is None:
            raise ValueError("current training-plan record not found")
        raw_sessions = plan.data.get("sessions")
        if not isinstance(raw_sessions, list):
            raise ValueError("plan sessions are unavailable")
        session = next(
            (
                item
                for item in raw_sessions
                if isinstance(item, dict) and str(item.get("session_id")) == session_id
            ),
            None,
        )
        if session is None:
            raise ValueError("planned session not found")
        activity = self._current("activity", activity_id)
        planned_sport = normalize_sport(session.get("sport"))
        actual_sport = normalize_sport(activity.data.get("sport"))
        planned_duration = session.get("duration_min")
        actual_duration = _number(activity.data, "duration_min")
        planned_day = str(session.get("session_date"))
        actual_day = activity.occurred_at.astimezone(self.timezone).date().isoformat()
        delta = (
            round(actual_duration - float(planned_duration), 2)
            if isinstance(planned_duration, (int, float))
            and not isinstance(planned_duration, bool)
            and actual_duration is not None
            else None
        )
        gaps = []
        if actual_duration is None:
            gaps.append("actual duration unavailable")
        if planned_sport == "other" or actual_sport == "other":
            gaps.append("sport identity is not normalized")
        return {
            "plan_id": plan_id,
            "session_id": session_id,
            "activity_id": activity_id,
            "planned": {
                "date": planned_day,
                "sport": planned_sport,
                "duration_min": planned_duration,
                "intensity": session.get("intensity"),
            },
            "actual": {
                "date": actual_day,
                "sport": actual_sport,
                "duration_min": actual_duration,
            },
            "same_day": planned_day == actual_day,
            "same_sport": planned_sport == actual_sport and planned_sport != "other",
            "duration_delta_min": delta,
            "quality_flags": list(activity.quality_flags),
            "data_gaps": gaps,
            "interpretation": (
                "descriptive comparison only; completion, effort and reason require user feedback"
            ),
        }

    @staticmethod
    def _raw_lap_count(record: CanonicalRecord) -> int:
        count = record.data.get("lap_count")
        if type(count) is int and count >= 0:
            return count
        laps = record.data.get("laps")
        return len(laps) if isinstance(laps, list) else 0

    @staticmethod
    def _laps(record: CanonicalRecord) -> list[dict[str, JsonValue]]:
        raw = record.data.get("laps")
        if not isinstance(raw, list):
            return []
        selected: list[dict[str, JsonValue]] = []
        for index, lap in enumerate(raw[:_MAX_LAPS], start=1):
            if not isinstance(lap, dict):
                continue
            selected.append({
                "index": index,
                **{
                    key: value
                    for key in ("duration_min", "distance_m", "avg_hr", "avg_power_w")
                    if (value := _number(lap, key)) is not None
                },
            })
        return selected

    @staticmethod
    def _gaps(
        sport: str, record: CanonicalRecord, laps: list[dict[str, JsonValue]]
    ) -> list[str]:
        gaps = []
        if sport == "other":
            gaps.append("sport type unrecognized")
        if _number(record.data, "duration_min") is None:
            gaps.append("duration unavailable")
        if _number(record.data, "distance_m") is None:
            gaps.append("distance unavailable")
        if not laps:
            gaps.append("lap or interval details unavailable")
        if record.data.get("detail_status") == "partial":
            gaps.append("some Garmin activity detail sections unavailable")
        if sport == "cycling" and _number(record.data, "avg_power_w") is None:
            gaps.append("cycling power unavailable")
        if sport == "swimming" and record.data.get("pool_length_m") is None:
            gaps.append("pool length or open-water context unavailable")
        return gaps
