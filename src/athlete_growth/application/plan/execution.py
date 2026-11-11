"""Explicitly link a planned session to actual activity and athlete feedback."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from athlete_growth.application.performance.service import (
    EndurancePerformanceService,
    normalize_sport,
)
from athlete_growth.domain.records import CanonicalRecord, JsonValue
from athlete_growth.ports.repositories import CanonicalRecordRepository

_STATUSES = {"completed", "partial", "skipped", "substituted"}


class PlanExecutionService:
    def __init__(
        self,
        athlete_id: str,
        records: CanonicalRecordRepository,
        *,
        timezone_name: str,
        now: datetime | None = None,
    ) -> None:
        self.athlete_id = athlete_id
        self.records = records
        self.timezone = ZoneInfo(timezone_name)
        self.now = now or datetime.now(UTC)
        self.performance = EndurancePerformanceService(
            athlete_id, records, timezone_name=timezone_name
        )

    def _plan_session(self, plan_id: str, session_id: str) -> tuple[CanonicalRecord, dict]:
        plan = next(
            (item for item in self.records.list_current(self.athlete_id, kind="training-plan")
             if item.data.get("plan_id") == plan_id), None
        )
        if plan is None:
            raise ValueError("confirmed plan not found")
        sessions = plan.data.get("sessions")
        if not isinstance(sessions, list):
            raise ValueError("confirmed plan has no sessions")
        session = next(
            (item for item in sessions
             if isinstance(item, dict) and item.get("session_id") == session_id), None
        )
        if session is None:
            raise ValueError("planned session not found")
        return plan, session

    @staticmethod
    def _kind(plan_id: str, session_id: str) -> str:
        return f"plan-execution:{plan_id}:{session_id}"

    def list(self, plan_id: str) -> dict[str, object]:
        plan = next(
            (item for item in self.records.list_current(self.athlete_id, kind="training-plan")
             if item.data.get("plan_id") == plan_id), None
        )
        if plan is None:
            raise ValueError("confirmed plan not found")
        prefix = f"plan-execution:{plan_id}:"
        entries = [
            {"record_id": item.record_id, **item.data}
            for item in self.records.list_current(self.athlete_id)
            if item.kind.startswith(prefix)
        ]
        pending, unplanned = self._pending_reviews(plan, entries)
        return {
            "plan_id": plan_id,
            "executions": entries,
            "count": len(entries),
            "pending_reviews": pending,
            "unplanned_activities": unplanned,
            "interpretation": (
                "Same-day or adjacent-day activity links are suggestions only. "
                "The athlete must confirm "
                "the linked session, completion status, effort and symptoms."
            ),
        }

    def _pending_reviews(
        self, plan: CanonicalRecord, entries: list[dict[str, object]]
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        raw_sessions = plan.data.get("sessions")
        if not isinstance(raw_sessions, list):
            raise ValueError("confirmed plan has no sessions")
        today = self.now.astimezone(self.timezone).date()
        confirmed_sessions = {str(item.get("session_id")) for item in entries}
        linked_activity_ids = {
            str(item.data["activity_id"])
            for item in self.records.list_current(self.athlete_id)
            if item.kind.startswith("plan-execution:") and item.data.get("activity_id")
        }
        sessions_by_day: dict[date, list[dict[str, JsonValue]]] = {}
        for raw in raw_sessions:
            if not isinstance(raw, dict) or not isinstance(raw.get("session_id"), str):
                continue
            try:
                day = date.fromisoformat(str(raw["session_date"]))
            except (KeyError, ValueError):
                continue
            sessions_by_day.setdefault(day, []).append(raw)
        start_raw = plan.data.get("start_date")
        try:
            start = date.fromisoformat(str(start_raw)) if start_raw else min(sessions_by_day)
        except (ValueError, TypeError):
            return [], []
        end = start + timedelta(days=6)
        activities_by_day: dict[date, list[CanonicalRecord]] = {}
        for activity in self.records.list_current(self.athlete_id, kind="activity"):
            if activity.record_id in linked_activity_ids:
                continue
            day = activity.occurred_at.astimezone(self.timezone).date()
            if start <= day <= min(end, today):
                activities_by_day.setdefault(day, []).append(activity)

        choices: list[tuple[date, dict[str, JsonValue], list[CanonicalRecord]]] = []
        candidate_counts: dict[str, int] = {}
        for day, sessions in sorted(sessions_by_day.items()):
            if day > today:
                continue
            same_day = sorted(activities_by_day.get(day, []), key=lambda item: item.record_id)
            for session in sessions:
                if str(session["session_id"]) in confirmed_sessions:
                    continue
                sport = normalize_sport(session.get("sport"))
                matching = [
                    activity for activity in same_day
                    if sport != "other" and normalize_sport(activity.data.get("sport")) == sport
                ]
                options = matching or same_day
                if not options and sport != "other":
                    options = sorted(
                        (
                            activity
                            for adjacent in (day - timedelta(days=1), day + timedelta(days=1))
                            for activity in activities_by_day.get(adjacent, [])
                            if normalize_sport(activity.data.get("sport")) == sport
                        ),
                        key=lambda item: item.record_id,
                    )
                choices.append((day, session, options))
                for activity in options:
                    candidate_counts[activity.record_id] = (
                        candidate_counts.get(activity.record_id, 0) + 1
                    )

        pending: list[dict[str, object]] = []
        candidate_ids = set(candidate_counts)
        for day, session, options in choices:
            unique = len(options) == 1 and candidate_counts[options[0].record_id] == 1
            moved = unique and options[0].occurred_at.astimezone(self.timezone).date() != day
            match_state = (
                "suggested_moved" if moved else "suggested" if unique
                else "ambiguous" if options else "no_activity"
            )
            pending.append({
                "session_id": session["session_id"],
                "session_date": day.isoformat(),
                "planned_sport": normalize_sport(session.get("sport")),
                "planned_duration_min": session.get("duration_min"),
                "planned_distance_m": session.get("distance_m"),
                "session_order": session.get("session_order", 1),
                "brick_id": session.get("brick_id"),
                "match_state": match_state,
                "activity_candidates": [
                    {
                        "activity_id": activity.record_id,
                        "activity_date": activity.occurred_at.astimezone(self.timezone)
                        .date().isoformat(),
                        "sport": normalize_sport(activity.data.get("sport")),
                        "duration_min": activity.data.get("duration_min"),
                        "distance_m": activity.data.get("distance_m"),
                        "same_sport": normalize_sport(activity.data.get("sport"))
                        == normalize_sport(session.get("sport")),
                    }
                    for activity in options
                ],
            })
        unplanned = [
            {
                "activity_id": activity.record_id,
                "activity_date": day.isoformat(),
                "sport": normalize_sport(activity.data.get("sport")),
                "duration_min": activity.data.get("duration_min"),
            }
            for day, activities in sorted(activities_by_day.items())
            for activity in sorted(activities, key=lambda item: item.record_id)
            if activity.record_id not in candidate_ids
        ]
        return pending, unplanned

    def _candidate(
        self,
        *,
        plan_id: str,
        session_id: str,
        status: str,
        activity_id: str | None,
        rpe: int | None,
        pain_after: int | None,
        reason: str | None,
    ) -> tuple[dict[str, object], str]:
        plan, session = self._plan_session(plan_id, session_id)
        if status not in _STATUSES:
            raise ValueError("unsupported execution status")
        if activity_id and status == "skipped":
            raise ValueError("skipped session cannot be linked to an activity")
        if rpe is not None and (type(rpe) is not int or not 0 <= rpe <= 10):
            raise ValueError("RPE must be from 0 to 10")
        if pain_after is not None and (
            type(pain_after) is not int or not 0 <= pain_after <= 10
        ):
            raise ValueError("pain must be from 0 to 10")
        if reason is not None and len(reason.strip()) > 500:
            raise ValueError("execution reason is too long")
        comparison = (
            self.performance.compare_plan_actual(plan_id, session_id, activity_id)
            if activity_id else None
        )
        activity_record_id = comparison["activity_id"] if comparison else None
        kind = self._kind(plan_id, session_id)
        if activity_record_id and any(
            item.kind.startswith("plan-execution:")
            and item.kind != kind
            and item.data.get("activity_id") == activity_record_id
            for item in self.records.list_current(self.athlete_id)
        ):
            raise ValueError("activity is already linked to another planned session")
        current = self.records.list_current(self.athlete_id, kind=kind)
        if len(current) > 1:
            raise ValueError("multiple current execution versions")
        previous = current[0].record_id if current else None
        proposed = {
            "plan_id": plan_id,
            "plan_record_id": plan.record_id,
            "session_id": session_id,
            "activity_id": activity_record_id,
            "status": status,
            "rpe": rpe,
            "pain_after": pain_after,
            "reason": reason.strip() if reason else None,
            "comparison": comparison,
            "evidence_status": "activity_and_user_report" if activity_id else "user_report_only",
        }
        payload = {
            "athlete_id": self.athlete_id,
            "previous_record_id": previous,
            "proposed": proposed,
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        return {
            "current_record_id": previous,
            "planned_session": session,
            "proposed": proposed,
            "requires_confirmation": True,
            "interpretation": (
                "Activity matching and completion are athlete-confirmed; "
                "device data alone does not establish effort or reason."
            ),
        }, digest

    def preview(self, **values: object) -> dict[str, object]:
        candidate, digest = self._candidate(**values)  # type: ignore[arg-type]
        return {**candidate, "preview_hash": digest, "saved": False}

    def confirm(self, *, preview_hash: str, **values: object) -> dict[str, object]:
        candidate, expected = self._candidate(**values)  # type: ignore[arg-type]
        if preview_hash != expected:
            current_id = candidate["current_record_id"]
            current = self.records.get(str(current_id)) if current_id else None
            if current is not None and current.data.get("confirmed_preview_hash") == preview_hash:
                return {"saved": False, "already_confirmed": True, "record_id": current_id}
            raise ValueError("execution evidence changed; preview again")
        proposed = cast(dict[str, JsonValue], candidate["proposed"])
        prior = cast(str | None, candidate["current_record_id"])
        record = CanonicalRecord.create(
            athlete_id=self.athlete_id,
            kind=self._kind(str(proposed["plan_id"]), str(proposed["session_id"])),
            occurred_at=datetime.now(UTC),
            data={**proposed, "confirmed_preview_hash": preview_hash},
            supersedes_id=prior,
        )
        saved = self.records.replace_singleton(self.athlete_id, record.kind, prior, record)
        return {"saved": True, "record_id": saved.record_id, "execution": proposed}
