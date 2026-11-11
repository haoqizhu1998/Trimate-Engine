"""Product-owned goal co-creation and two-step training-plan workflow."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from athlete_growth.application.calibration import SportCalibrationService
from athlete_growth.application.performance.service import normalize_sport
from athlete_growth.application.plan.adjustments import adjust_upcoming_session
from athlete_growth.application.plan.blocks import (
    add_progression_envelope,
    build_block_cycle,
    choose_key_windows,
    focus_for_date,
)
from athlete_growth.application.plan.requested import requested_sessions_for_week
from athlete_growth.application.plan.service import TrainingPlanner
from athlete_growth.application.preferences import current_training_preferences
from athlete_growth.application.report import ReportService
from athlete_growth.domain.goals import Goal, GoalType, detect_conflicts
from athlete_growth.domain.records import CanonicalRecord, JsonValue
from athlete_growth.domain.safety import RiskLevel
from athlete_growth.domain.training import PlannedSession, TrainingConstraints, TrainingPlan
from athlete_growth.ports.planning import PlanningDraft, PlanningDraftRepository
from athlete_growth.ports.repositories import CanonicalRecordRepository


def _json_data(value: object) -> dict[str, JsonValue]:
    normalized = json.loads(json.dumps(value, ensure_ascii=False, default=str))
    if not isinstance(normalized, dict):
        raise TypeError("planning data must be an object")
    return cast(dict[str, JsonValue], normalized)


def _constraints_data(constraints: TrainingConstraints) -> dict[str, object]:
    return {
        "available_minutes_by_weekday": constraints.available_minutes_by_weekday,
        "unavailable_weekdays": sorted(constraints.unavailable_weekdays),
        "max_weekly_minutes": constraints.max_weekly_minutes,
        "allowed_sports": list(constraints.allowed_sports),
        "preferred_quality_weekday": constraints.preferred_quality_weekday,
        "preferred_long_weekday": constraints.preferred_long_weekday,
        "focus_sport": constraints.focus_sport,
        "aerobic_mixing": constraints.aerobic_mixing,
        "use_saved_preferences": constraints.use_saved_preferences,
        "phase_ceiling_preapplied": constraints.phase_ceiling_preapplied,
    }


class PlanningWorkflow:
    """Draft first, confirm explicitly; never depend on a model conversation as storage."""

    def __init__(
        self,
        athlete_id: str,
        records: CanonicalRecordRepository,
        drafts: PlanningDraftRepository,
        *,
        timezone_name: str = "UTC",
        clock: datetime | None = None,
    ) -> None:
        self.athlete_id = athlete_id
        self.records = records
        self.drafts = drafts
        self.timezone = ZoneInfo(timezone_name)
        self.clock = clock

    def _now(self) -> datetime:
        return self.clock or datetime.now(UTC)

    def list_open_drafts(self) -> dict[str, object]:
        drafts: list[dict[str, object]] = []
        for draft in self.drafts.list_open(self.athlete_id):
            if draft.kind == "goal" and draft.data.get("action") in {"replace", "delete"}:
                drafts.append(self._goal_change_view(draft))
            elif draft.kind == "goal":
                drafts.append(self._goal_view(draft))
            else:
                drafts.append(
                    {
                        "preview_id": draft.draft_id,
                        "revision": draft.revision,
                        "status": draft.status,
                        "goal_id": draft.data.get("goal_id"),
                        "plan": draft.data.get("plan"),
                        "summary": draft.data.get("summary"),
                        "assumptions": draft.data.get("assumptions"),
                        "goal_conflicts": draft.data.get("goal_conflicts"),
                        "replanning_context": draft.data.get("replanning_context"),
                        "adjustment_context": draft.data.get("adjustment_context"),
                        "block_cycle": draft.data.get("block_cycle"),
                        "data_gaps": draft.data.get("data_gaps"),
                        "confidence": draft.data.get("confidence"),
                        "unscheduled_sports": draft.data.get("unscheduled_sports"),
                        "requires_confirmation": True,
                    }
                )
        return {"drafts": drafts}

    def propose_goal(
        self,
        *,
        idea: str | None = None,
        proposal_id: str | None = None,
        expected_revision: int | None = None,
        goal_type: str | None = None,
        title: str | None = None,
        priority: int | None = None,
        deadline: date | None = None,
        target_metric: str | None = None,
        target_value: float | None = None,
    ) -> dict[str, object]:
        if goal_type is not None and goal_type not in {"sport", "body", "race"}:
            raise ValueError("goal_type must be sport, body, or race")
        if priority is not None and not 1 <= priority <= 5:
            raise ValueError("priority must be between 1 and 5")
        if target_value is not None and target_metric is None and proposal_id is None:
            raise ValueError("target_value requires target_metric")
        if target_value is not None and not math.isfinite(target_value):
            raise ValueError("target_value must be finite")
        if proposal_id is None:
            if idea is None or not idea.strip():
                raise ValueError("tell us what you want to work toward")
            if len(idea.strip()) > 1000:
                raise ValueError("goal idea is too long")
            explicit = [
                name
                for name, value in (
                    ("goal_type", goal_type),
                    ("title", title),
                    ("priority", priority),
                    ("deadline", deadline),
                    ("target_metric", target_metric),
                    ("target_value", target_value),
                )
                if value is not None
            ]
            data = _json_data(
                {
                    "idea": idea.strip(),
                    "goal_type": goal_type,
                    "title": title.strip() if title else idea.strip(),
                    "priority": priority if priority is not None else 3,
                    "deadline": deadline,
                    "target_metric": target_metric,
                    "target_value": target_value,
                    "explicit_fields": explicit,
                }
            )
            draft = PlanningDraft(uuid4().hex, self.athlete_id, "goal", 1, "draft", data)
            self.drafts.create(draft)
        else:
            if expected_revision is None:
                raise ValueError("expected_revision is required when revising a goal")
            previous = self._draft("goal", proposal_id)
            if previous.status != "draft":
                raise ValueError("confirmed goals cannot be revised as drafts")
            if previous.revision != expected_revision:
                raise ValueError("draft revision changed; review the latest draft")
            data = dict(previous.data)
            explicit_value = data.get("explicit_fields")
            explicit = (
                {str(item) for item in explicit_value}
                if isinstance(explicit_value, list)
                else set()
            )
            updates: dict[str, object | None] = {
                "goal_type": goal_type,
                "title": title.strip() if title is not None else None,
                "priority": priority,
                "deadline": deadline,
                "target_metric": target_metric,
                "target_value": target_value,
            }
            if idea is not None:
                if not idea.strip() or len(idea.strip()) > 1000:
                    raise ValueError("goal idea must contain 1-1000 characters")
                if "title" not in explicit:
                    data["title"] = idea.strip()
                data["idea"] = idea.strip()
            for key, value in updates.items():
                if value is not None:
                    data[key] = cast(
                        JsonValue, value.isoformat() if isinstance(value, date) else value
                    )
                    explicit.add(key)
            data["explicit_fields"] = cast(JsonValue, sorted(explicit))
            if data.get("target_value") is not None and not data.get("target_metric"):
                raise ValueError("target_value requires target_metric")
            draft = PlanningDraft(
                previous.draft_id,
                self.athlete_id,
                "goal",
                previous.revision + 1,
                "draft",
                data,
            )
            self.drafts.revise(draft, expected_revision=expected_revision)
        return self._goal_view(draft)

    def confirm_goal(self, proposal_id: str, revision: int) -> dict[str, object]:
        draft = self._draft("goal", proposal_id)
        if draft.data.get("action") in {"replace", "delete"}:
            raise ValueError("use confirm_goal_change for a goal change")
        if draft.revision != revision:
            raise ValueError("draft revision changed; review the latest draft")
        if draft.status == "confirmed":
            record = self._confirmed_record(draft)
            return {
                "goal": record.data,
                "record_id": record.record_id,
                "confirmed": True,
                "plan_review_required": bool(
                    self.records.list_current(self.athlete_id, kind="training-plan")
                ),
            }
        view = self._goal_view(draft)
        if view["missing_fields"]:
            raise ValueError("goal draft is incomplete; review missing fields")
        data = draft.data
        goal = Goal(
            goal_id=f"goal-{draft.draft_id}",
            athlete_id=self.athlete_id,
            goal_type=cast(GoalType, str(data["goal_type"])),
            title=str(data["title"]),
            priority=int(str(data["priority"])),
            deadline=date.fromisoformat(str(data["deadline"])) if data.get("deadline") else None,
            target_metric=str(data["target_metric"]) if data.get("target_metric") else None,
            target_value=float(str(data["target_value"]))
            if data.get("target_value") is not None
            else None,
        )
        record = CanonicalRecord.create(
            athlete_id=self.athlete_id,
            kind="goal",
            occurred_at=self._now(),
            data=_json_data(
                {**asdict(goal), "source_idea": data["idea"], "proposal_id": proposal_id}
            ),
        )
        saved = self.drafts.confirm(self.athlete_id, "goal", proposal_id, revision, record)
        return {
            "goal": saved.data,
            "record_id": saved.record_id,
            "confirmed": True,
            "plan_review_required": bool(
                self.records.list_current(self.athlete_id, kind="training-plan")
            ),
        }

    def preview_goal_change(
        self,
        *,
        action: str,
        goal_id: str,
        reason: str,
        goal_type: str | None = None,
        title: str | None = None,
        priority: int | None = None,
        deadline: date | None = None,
        target_metric: str | None = None,
        target_value: float | None = None,
    ) -> dict[str, object]:
        if action not in {"replace", "delete"}:
            raise ValueError("action must be replace or delete")
        if not reason.strip():
            raise ValueError("goal change reason is required")
        if len(reason) > 1000:
            raise ValueError("goal change reason is too long")
        target = self._goal_record(goal_id)
        if target is None:
            raise KeyError("confirmed goal not found")
        if action == "replace":
            if goal_type not in {"sport", "body", "race"} or not title or not title.strip():
                raise ValueError("replacement goal needs type and title")
            if priority is None or not 1 <= priority <= 5:
                raise ValueError("replacement goal needs priority between 1 and 5")
            if target_value is not None and (not target_metric or not math.isfinite(target_value)):
                raise ValueError("replacement target requires a finite metric and value")
        elif any(
            value is not None
            for value in (goal_type, title, priority, deadline, target_metric, target_value)
        ):
            raise ValueError("delete does not accept replacement fields")
        preview_id = uuid4().hex
        replacement = (
            asdict(
                Goal(
                    f"goal-{preview_id}",
                    self.athlete_id,
                    cast(GoalType, goal_type),
                    title.strip(),
                    cast(int, priority),
                    deadline,
                    target_metric,
                    target_value,
                )
            )
            if action == "replace"
            else None
        )
        draft = PlanningDraft(
            preview_id,
            self.athlete_id,
            "goal",
            1,
            "draft",
            _json_data(
                {
                    "action": action,
                    "target_goal_id": goal_id,
                    "target_record_id": target.record_id,
                    "target_goal": target.data,
                    "replacement_goal": replacement,
                    "reason": reason.strip(),
                    "data_revision": self.records.list_current_ids(self.athlete_id),
                    "affected_plan_ids": [
                        str(item.data["plan_id"]) for item in self._plans_for_goal(goal_id)
                    ],
                }
            ),
        )
        self.drafts.create(draft)
        return self._goal_change_view(draft)

    def confirm_goal_change(self, preview_id: str, revision: int) -> dict[str, object]:
        draft = self._draft("goal", preview_id)
        if draft.data.get("action") not in {"replace", "delete"}:
            raise ValueError("use confirm_goal for a new goal")
        if draft.revision != revision:
            raise ValueError("draft revision changed; review the latest draft")
        if draft.status == "confirmed":
            saved = self._confirmed_record(draft)
            return {
                "action": draft.data["action"],
                "goal": saved.data if draft.data["action"] == "replace" else None,
                "record_id": saved.record_id,
                "confirmed": True,
                "plan_review_required": True,
            }
        replacement_data = draft.data.get("replacement_goal")
        replacement = (
            CanonicalRecord.create(
                athlete_id=self.athlete_id,
                kind="goal",
                occurred_at=self._now(),
                data=_json_data(
                    {
                        **cast(dict[str, object], replacement_data),
                        "replaces_goal_id": draft.data["target_goal_id"],
                        "change_reason": draft.data["reason"],
                    }
                ),
            )
            if isinstance(replacement_data, dict)
            else None
        )
        revision_ids = draft.data.get("data_revision")
        if not isinstance(revision_ids, list):
            raise ValueError("goal change preview has no data revision")
        saved = self.drafts.confirm_goal_change(
            self.athlete_id,
            preview_id,
            revision,
            str(draft.data["target_record_id"]),
            replacement,
            expected_current_ids=tuple(str(item) for item in revision_ids),
        )
        return {
            "action": draft.data["action"],
            "goal": saved.data if replacement else None,
            "record_id": saved.record_id,
            "confirmed": True,
            "plan_review_required": True,
        }

    def _goal_change_view(self, draft: PlanningDraft) -> dict[str, object]:
        return {
            "preview_id": draft.draft_id,
            "revision": draft.revision,
            "status": draft.status,
            "action": draft.data["action"],
            "target_goal": draft.data["target_goal"],
            "replacement_goal": draft.data.get("replacement_goal"),
            "reason": draft.data["reason"],
            "affected_plan_ids": draft.data["affected_plan_ids"],
            "plan_review_required": True,
            "requires_confirmation": True,
        }

    def _goal_record(self, goal_id: str) -> CanonicalRecord | None:
        return next(
            (
                item
                for item in self.records.list_current(self.athlete_id, kind="goal")
                if item.data.get("goal_id") == goal_id
                and item.data.get("status", "active") == "active"
            ),
            None,
        )

    def preview_plan(
        self,
        *,
        goal_id: str,
        start_date: date,
        constraints: TrainingConstraints,
        requested_sessions: tuple[dict[str, object], ...] = (),
    ) -> dict[str, object]:
        requested_constraints = constraints
        saved_preference = (
            current_training_preferences(self.records, self.athlete_id)
            if constraints.use_saved_preferences
            else {"record_id": None, "preferences": {}, "source": "disabled"}
        )
        saved_values = saved_preference["preferences"]
        assert isinstance(saved_values, dict)
        preference_fields_applied = [
            field
            for field in ("preferred_quality_weekday", "preferred_long_weekday", "aerobic_mixing")
            if getattr(constraints, field) is None and field in saved_values
        ]
        chosen_focus = constraints.focus_sport or saved_values.get("focus_sport")
        if chosen_focus not in constraints.allowed_sports:
            chosen_focus = None
        elif constraints.focus_sport is None and "focus_sport" in saved_values:
            preference_fields_applied.append("focus_sport")
        constraints = replace(
            constraints,
            preferred_quality_weekday=(
                constraints.preferred_quality_weekday
                if constraints.preferred_quality_weekday is not None
                else saved_values.get("preferred_quality_weekday")
            ),
            preferred_long_weekday=(
                constraints.preferred_long_weekday
                if constraints.preferred_long_weekday is not None
                else saved_values.get("preferred_long_weekday")
            ),
            focus_sport=cast(str | None, chosen_focus),
            aerobic_mixing=(
                constraints.aerobic_mixing
                if constraints.aerobic_mixing is not None
                else bool(saved_values.get("aerobic_mixing", True))
            ),
        )
        goal = next((goal for goal in self._goals() if goal.goal_id == goal_id), None)
        if goal is None:
            raise KeyError("confirmed goal not found")
        today = self._now().astimezone(self.timezone).date()
        if start_date < today:
            raise ValueError("plan start date cannot be in the past")
        if constraints.max_weekly_minutes <= 0 or not constraints.allowed_sports:
            raise ValueError("weekly minutes and at least one sport are required")
        event_day = (
            goal.deadline
            if goal.goal_type == "race"
            and goal.deadline is not None
            and start_date <= goal.deadline <= start_date + timedelta(days=6)
            else None
        )
        taper_week = (
            goal.goal_type == "race"
            and goal.deadline is not None
            and start_date + timedelta(days=7) <= goal.deadline <= start_date + timedelta(days=13)
        )
        if event_day is not None:
            constraints = replace(
                constraints,
                max_weekly_minutes=(
                    constraints.max_weekly_minutes
                    if constraints.phase_ceiling_preapplied
                    else max(1, round(constraints.max_weekly_minutes * 0.6))
                ),
                unavailable_weekdays=constraints.unavailable_weekdays | {event_day.weekday()},
                preferred_quality_weekday=None,
                preferred_long_weekday=None,
            )
        elif taper_week:
            constraints = replace(
                constraints,
                max_weekly_minutes=(
                    constraints.max_weekly_minutes
                    if constraints.phase_ceiling_preapplied
                    else max(1, round(constraints.max_weekly_minutes * 0.8))
                ),
                preferred_quality_weekday=None,
                preferred_long_weekday=None,
            )
        report = ReportService(
            self.athlete_id,
            self.records,
            timezone_name=self.timezone.key,
        ).daily(today)
        if report.safety.level == RiskLevel.HIGH:
            return {
                "blocked": True,
                "risk": asdict(report.safety),
                "data_gaps": report.data_gaps,
                "requires_confirmation": False,
            }
        existing = self._plans_for_goal(goal_id)
        previous = (
            max(existing, key=lambda item: int(str(item.data.get("version", 0))))
            if existing
            else None
        )
        recent_sessions: list[tuple[str, int, date]] = []
        excluded_activity_count = 0
        for item in self.records.list_current(self.athlete_id, kind="activity"):
            activity_day = item.occurred_at.astimezone(self.timezone).date()
            if not 0 < (start_date - activity_day).days <= 28:
                continue
            sport = normalize_sport(item.data.get("sport"))
            if sport not in constraints.allowed_sports:
                continue
            if item.quality_flags:
                excluded_activity_count += 1
                continue
            try:
                minutes = int(float(str(item.data.get("duration_min", 0))))
                if minutes > 0:
                    recent_sessions.append((sport, minutes, activity_day))
                else:
                    excluded_activity_count += 1
            except (TypeError, ValueError, OverflowError):
                excluded_activity_count += 1
                continue
        observed_sports = {item[0] for item in recent_sessions}
        assumptions = ["Expected load is planned minutes only, not TSS or physiological load."]
        if event_day is not None:
            assumptions.append(
                "Race week is a reduced-volume preview; the event day is reserved for a separate "
                "race strategy and is not counted as a training session. The provisional 60% "
                "ceiling is a product heuristic, not an individualized taper prescription."
            )
        elif taper_week:
            assumptions.append(
                "The week before the race uses a provisional 80% volume ceiling and no key "
                "quality or long session. This is a product heuristic; recheck against the "
                "event, recent training and recovery."
            )
        if constraints.use_saved_preferences and saved_preference["record_id"] is not None:
            assumptions.append(
                "Confirmed profile preferences were applied where no plan override was provided."
            )
        if saved_values.get("focus_sport") and chosen_focus is None:
            assumptions.append("Saved focus sport is outside the selected sports and was ignored.")
        if saved_values.get("key_session_pattern") == "focus_blocks":
            assumptions.append(
                "Focus-block preference is recorded; use preview_plan_blocks to rotate weeks."
            )
        recent_week = [
            item for item in recent_sessions if (start_date - item[2]).days <= 7
        ]
        planner = TrainingPlanner()
        volume = planner.volume_decision(
            start_date=start_date,
            recent_sessions=tuple(recent_sessions),
            constraints=constraints,
            safety=report.safety,
        )
        if not recent_sessions and not requested_sessions:
            assumptions.append(
                "No recent training duration was found; preview is limited to two short "
                "easy sessions, or three when three disciplines are requested, with a "
                "30-minute single-session ceiling. This is a product starting assumption, "
                "not an individual safety threshold."
            )
        elif not recent_week and not requested_sessions:
            assumptions.append(
                "No activity was found in the last seven days. Earlier peak weeks are not "
                "used to restore training volume; this restart preview is limited to short "
                "easy sessions with a 30-minute single-session ceiling."
            )
        elif volume.mode in {"progressive_build", "return_to_baseline"}:
            assumptions.append(
                "This week's volume budget uses the last four observed seven-day windows "
                "and current safety status. The increase is a reviewable product planning "
                "heuristic, not a proven safe percentage or a requirement to fill every minute."
            )
        elif volume.mode == "recent_spike_hold" and not requested_sessions:
            assumptions.append(
                "The latest week is much higher than the preceding training pattern; "
                "the preview does not add more volume on top of that spike."
            )
        elif volume.mode == "safety_hold" and not requested_sessions:
            assumptions.append(
                "Current recovery evidence is incomplete or elevated, so this preview "
                "does not increase the observed weekly volume."
            )
        if excluded_activity_count:
            assumptions.append(
                "Activities with quality flags or invalid durations were excluded from "
                "the planning baseline."
            )
        if requested_sessions and not recent_sessions:
            assumptions.append(
                "No recent training baseline was found. The explicit schedule is a user "
                "request and may exceed the automatic short easy starting plan."
            )
        for sport in constraints.allowed_sports:
            if sport not in observed_sports and recent_sessions and not requested_sessions:
                assumptions.append(
                    f"No recent {sport} session was found; that sport uses a provisional "
                    "30-minute single-session ceiling."
                )
        generated = planner.generate(
            athlete_id=self.athlete_id,
            goal=goal,
            constraints=constraints,
            safety=report.safety,
            start_date=start_date,
            recent_sessions=tuple(recent_sessions),
        )
        if requested_sessions:
            explicit = requested_sessions_for_week(
                requested_sessions, start_date=start_date, constraints=constraints
            )
            if report.safety.level != RiskLevel.LOW and any(
                item.intensity == "quality" for item in explicit
            ):
                raise ValueError("quality sessions need a low-risk current assessment")
            generated = replace(
                generated,
                sessions=explicit,
                change_reason="user-specified seven-day session structure",
            )
            assumptions.append(
                "Session dates, order, durations and distances are user-specified; "
                "distance_m is not inferred from pace or speed. Review each leg before approval."
            )
            if sum(item.duration_min for item in explicit) > volume.target_minutes:
                assumptions.append(
                    "Requested weekly minutes exceed the observed-evidence volume budget; "
                    "review training history, recovery and schedule before approval."
                )
        if not generated.sessions:
            raise ValueError("no sessions fit the available time and hard constraints")
        unscheduled_sports = [
            sport
            for sport in constraints.allowed_sports
            if sport not in {session.sport for session in generated.sessions}
        ]
        preview_id = uuid4().hex
        plan = replace(
            generated,
            plan_id=f"plan-{preview_id}",
            version=int(str(previous.data["version"])) + 1 if previous else 1,
            created_at=self._now(),
            previous_plan_id=str(previous.data["plan_id"]) if previous else None,
        )
        per_sport_minutes = {
            sport: sum(session.duration_min for session in plan.sessions if session.sport == sport)
            for sport in constraints.allowed_sports
        }
        summary = {
            "total_minutes": sum(per_sport_minutes.values()),
            "per_sport_minutes": per_sport_minutes,
            "key_sessions": sum(
                session.session_type in {"quality", "long"} for session in plan.sessions
            ),
            "distance": (
                "user-specified distance_m per session; missing distances remain unspecified"
                if any(session.distance_m is not None for session in plan.sessions)
                else "not prescribed without a validated personal pace or speed baseline"
            ),
            "per_sport_distance_m": {
                sport: sum(
                    session.distance_m or 0 for session in plan.sessions
                    if session.sport == sport
                ) for sport in constraints.allowed_sports
            },
            "sessions_with_distance": sum(
                session.distance_m is not None for session in plan.sessions
            ),
            "brick_count": len({
                session.brick_id for session in plan.sessions if session.brick_id
            }),
            "two_session_days": [
                day.isoformat() for day in sorted({item.session_date for item in plan.sessions})
                if sum(item.session_date == day for item in plan.sessions) >= 2
            ],
        }
        calibration_service = SportCalibrationService(self.athlete_id, self.records, today=today)
        calibration_by_sport = {
            sport: calibration_service.get(sport) for sport in constraints.allowed_sports
        }
        intensity_guidance = {
            session.session_id: {
                "sport": session.sport,
                "calibration_record_id": reference["record_id"],
                "metric": reference["metric"],
                "band": cast(dict[str, object], reference["reference_bands"])[
                    "threshold" if session.intensity == "quality" else "easy"
                ],
                "interpretation": (
                    "Broad reference only; use effort and current readiness to adjust."
                ),
            }
            for session in plan.sessions
            if (reference := calibration_by_sport[session.sport])["status"] == "current"
            and session.intensity in {"quality", "easy"}
        }
        if any(item["status"] != "current" for item in calibration_by_sport.values()):
            assumptions.append(
                "Missing or stale sport calibration: use effort and talk test, "
                "not exact pace or power."
            )
        current_goals = self.records.list_current(self.athlete_id, kind="goal")
        goal_conflicts = [asdict(item) for item in detect_conflicts(self._goals())]
        goal_record = self._goal_record(goal_id)
        assert goal_record is not None
        context = {
            "goal_record_id": goal_record.record_id,
            "active_goal_record_ids": [item.record_id for item in current_goals],
            "constraints": _constraints_data(constraints),
            "requested_constraints": _constraints_data(requested_constraints),
            "preference_record_id": (
                saved_preference["record_id"] if constraints.use_saved_preferences else None
            ),
            "preference_source": (
                "confirmed_profile" if preference_fields_applied else "plan_inputs_only"
            ),
            "preference_fields_applied": preference_fields_applied,
            "plan_override_fields": [
                field
                for field in (
                    "preferred_quality_weekday",
                    "preferred_long_weekday",
                    "focus_sport",
                    "aerobic_mixing",
                )
                if getattr(requested_constraints, field) is not None
            ],
            "key_session_pattern": saved_values.get("key_session_pattern"),
            "recent_activity_count": len(recent_sessions),
            "excluded_activity_count": excluded_activity_count,
            "recent_7d_activity_count": len(recent_week),
            "recent_7d_minutes": sum(item[1] for item in recent_week),
            "baseline_mode": volume.mode,
            "observed_week_minutes": list(volume.observed_week_minutes),
            "typical_prior_minutes": volume.typical_prior_minutes,
            "weekly_volume_budget_minutes": volume.target_minutes,
            "session_structure_source": (
                "user_specified" if requested_sessions else "generated"
            ),
            "event_day": event_day.isoformat() if event_day else None,
            "taper_week": taper_week,
            "calibration_record_ids": {
                sport: item["record_id"] for sport, item in calibration_by_sport.items()
            },
        }
        draft = PlanningDraft(
            preview_id,
            self.athlete_id,
            "plan",
            1,
            "draft",
            _json_data(
                {
                    "goal_id": goal_id,
                    "plan": asdict(plan),
                    "summary": summary,
                    "intensity_guidance": intensity_guidance,
                    "assumptions": assumptions,
                    "planning_context": context,
                    "goal_conflicts": goal_conflicts,
                    "start_date": start_date,
                    "safety_day": today,
                    "safety_level": report.safety.level.value,
                    "safety_ruleset_version": report.safety.ruleset_version,
                    "data_revision": self.records.list_current_ids(self.athlete_id),
                    "data_gaps": report.data_gaps,
                    "confidence": report.confidence,
                    "algorithm_version": TrainingPlanner.algorithm_version,
                    "requested_sports": constraints.allowed_sports,
                    "unscheduled_sports": unscheduled_sports,
                    "plan_scope": "seven-day evidence-bounded plan; no fixed multiweek progression",
                }
            ),
        )
        self.drafts.create(draft)
        return {
            "preview_id": preview_id,
            "revision": 1,
            "plan": draft.data["plan"],
            "summary": summary,
            "intensity_guidance": intensity_guidance,
            "assumptions": assumptions,
            "planning_context": context,
            "goal_conflicts": goal_conflicts,
            "risk": asdict(report.safety),
            "data_gaps": report.data_gaps,
            "confidence": report.confidence,
            "algorithm_version": TrainingPlanner.algorithm_version,
            "requested_sports": list(constraints.allowed_sports),
            "unscheduled_sports": unscheduled_sports,
            "plan_scope": "seven-day evidence-bounded plan; no fixed multiweek progression",
            "requires_confirmation": True,
            "blocked": False,
        }

    def preview_plan_blocks(
        self,
        *,
        goal_id: str,
        start_date: date,
        constraints: TrainingConstraints,
        weeks: int,
        focus_sequence: tuple[str, ...] = (),
        requested_sessions: tuple[dict[str, object], ...] = (),
    ) -> dict[str, object]:
        saved = (
            current_training_preferences(self.records, self.athlete_id)
            if constraints.use_saved_preferences
            else {"record_id": None, "preferences": {}}
        )
        values = saved["preferences"]
        assert isinstance(values, dict)
        if not focus_sequence and values.get("key_session_pattern") != "focus_blocks":
            raise ValueError("block rotation needs an explicit sequence or confirmed preference")
        quality_day = (
            constraints.preferred_quality_weekday
            if constraints.preferred_quality_weekday is not None
            else values.get("preferred_quality_weekday")
        )
        long_day = (
            constraints.preferred_long_weekday
            if constraints.preferred_long_weekday is not None
            else values.get("preferred_long_weekday")
        )
        quality_day, long_day, day_sources = choose_key_windows(
            start_date,
            constraints,
            quality_weekday=cast(int | None, quality_day),
            long_weekday=cast(int | None, long_day),
        )
        cycle = build_block_cycle(
            start_date=start_date,
            weeks=weeks,
            allowed_sports=constraints.allowed_sports,
            focus_sequence=focus_sequence,
            preferred_focus=constraints.focus_sport or cast(str | None, values.get("focus_sport")),
            quality_weekday=quality_day,
            long_weekday=long_day,
            key_window_sources=day_sources,
            source="explicit_sequence" if focus_sequence else "confirmed_profile",
            base_constraints=_constraints_data(constraints),
        )
        forecast_entries = cast(list[dict[str, object]], cycle["forecast"])
        first_focus = str(forecast_entries[0]["focus_sport"])
        first = replace(
            constraints,
            focus_sport=first_focus,
            preferred_quality_weekday=quality_day,
            preferred_long_weekday=long_day,
        )
        preview = self.preview_plan(
            goal_id=goal_id,
            start_date=start_date,
            constraints=first,
            requested_sessions=requested_sessions,
        )
        if preview.get("blocked"):
            return {**preview, "block_cycle": cycle}
        goal_record = self._goal_record(goal_id)
        assert goal_record is not None
        deadline_value = goal_record.data.get("deadline")
        goal_deadline = date.fromisoformat(str(deadline_value)) if deadline_value else None
        summary = preview["summary"]
        assert isinstance(summary, dict)
        planning_context = preview.get("planning_context")
        cycle = add_progression_envelope(
            cycle,
            observed_first_week_minutes=int(summary["total_minutes"]),
            goal_type=str(goal_record.data.get("goal_type")),
            goal_deadline=goal_deadline,
            progression_eligible=(
                isinstance(planning_context, dict)
                and planning_context.get("baseline_mode")
                in {"progressive_build", "return_to_baseline"}
            ),
        )
        draft = self._draft("plan", str(preview["preview_id"]))
        scope = "first seven days are a confirmable draft; later weeks are focus forecasts only"
        updated = PlanningDraft(
            draft.draft_id,
            self.athlete_id,
            "plan",
            draft.revision + 1,
            "draft",
            _json_data(
                {**draft.data, "block_cycle": cycle, "block_week_index": 0, "plan_scope": scope}
            ),
        )
        self.drafts.revise(updated, expected_revision=draft.revision)
        return {
            **preview,
            "revision": updated.revision,
            "block_cycle": cycle,
            "block_week_index": 0,
            "plan_scope": scope,
        }

    def preview_plan_adjustment(
        self,
        *,
        plan_id: str,
        session_id: str,
        action: str,
        reason: str,
        new_date: date | None = None,
        new_sport: str | None = None,
        new_duration_min: int | None = None,
    ) -> dict[str, object]:
        """Preview one user-requested change without rewriting completed or future weeks."""
        if not reason.strip() or len(reason) > 300:
            raise ValueError("plan adjustment reason is required and limited to 300 characters")
        prior = next(
            (
                item
                for item in self.records.list_current(self.athlete_id, kind="training-plan")
                if item.data.get("plan_id") == plan_id
            ),
            None,
        )
        if prior is None:
            raise KeyError("confirmed plan not found")
        goal_id = str(prior.data.get("goal_id"))
        latest = max(
            self._plans_for_goal(goal_id), key=lambda item: int(str(item.data.get("version", 0)))
        )
        if latest.record_id != prior.record_id:
            raise ValueError("plan version changed; preview the plan again")
        if self._goal_record(goal_id) is None:
            raise ValueError("goal is no longer active; preview the plan again")
        context = prior.data.get("planning_context")
        if not isinstance(context, dict) or not isinstance(context.get("constraints"), dict):
            raise ValueError("previous plan has no reusable constraints")
        raw = context["constraints"]
        raw_availability = raw.get("available_minutes_by_weekday")
        if not isinstance(raw_availability, dict):
            raise ValueError("previous plan has no reusable constraints")
        constraints = TrainingConstraints(
            {int(key): int(value) for key, value in raw_availability.items()},
            frozenset(int(item) for item in raw.get("unavailable_weekdays", [])),
            int(raw["max_weekly_minutes"]),
            tuple(str(item) for item in raw["allowed_sports"]),
        )
        today = self._now().astimezone(self.timezone).date()
        report = ReportService(
            self.athlete_id, self.records, timezone_name=self.timezone.key
        ).daily(today)
        if report.safety.level == RiskLevel.HIGH:
            return {
                "blocked": True,
                "risk": asdict(report.safety),
                "data_gaps": report.data_gaps,
                "requires_confirmation": False,
            }
        prior_plan = self._plan_from_data(cast(dict[str, Any], prior.data))
        if report.safety.level != RiskLevel.LOW and any(
            item.session_type in {"quality", "long"} for item in prior_plan.sessions
        ):
            return {
                "blocked": True,
                "risk": asdict(report.safety),
                "data_gaps": report.data_gaps,
                "next_step": "preview a full lower-intensity plan update before changing sessions",
                "requires_confirmation": False,
            }
        if not prior_plan.sessions:
            raise ValueError("plan sessions are unavailable")
        first_date = min(item.session_date for item in prior_plan.sessions)
        week_start = date.fromisoformat(str(prior.data.get("start_date", first_date.isoformat())))
        observed_longest: dict[str, int] = {}
        for item in self.records.list_current(self.athlete_id, kind="activity"):
            sport = str(item.data.get("sport"))
            try:
                minutes = int(float(str(item.data.get("duration_min", 0))))
            except (TypeError, ValueError):
                continue
            days_ago = (today - item.occurred_at.astimezone(self.timezone).date()).days
            if 0 <= days_ago <= 28 and minutes > 0:
                observed_longest[sport] = max(observed_longest.get(sport, 0), minutes)
        sessions, change = adjust_upcoming_session(
            prior_plan.sessions,
            session_id=session_id,
            action=action,
            today=today,
            constraints=constraints,
            week_start=week_start,
            new_date=new_date,
            new_sport=new_sport,
            new_duration_min=new_duration_min,
            observed_longest=observed_longest,
        )
        preview_id = uuid4().hex
        plan = replace(
            prior_plan,
            plan_id=f"plan-{preview_id}",
            version=prior_plan.version + 1,
            created_at=self._now(),
            sessions=sessions,
            status="draft",
            change_reason=reason.strip(),
            previous_plan_id=plan_id,
        )
        per_sport_minutes = {
            sport: sum(item.duration_min for item in sessions if item.sport == sport)
            for sport in constraints.allowed_sports
        }
        summary = {
            "total_minutes": sum(per_sport_minutes.values()),
            "per_sport_minutes": per_sport_minutes,
            "key_sessions": sum(item.session_type in {"quality", "long"} for item in sessions),
            "distance": (
                "user-specified distance_m per session; missing distances remain unspecified"
                if any(item.distance_m is not None for item in sessions)
                else "not prescribed without a validated personal pace or speed baseline"
            ),
            "per_sport_distance_m": {
                sport: sum(item.distance_m or 0 for item in sessions if item.sport == sport)
                for sport in constraints.allowed_sports
            },
            "sessions_with_distance": sum(item.distance_m is not None for item in sessions),
            "brick_count": len({item.brick_id for item in sessions if item.brick_id}),
            "two_session_days": [
                day.isoformat() for day in sorted({item.session_date for item in sessions})
                if sum(item.session_date == day for item in sessions) >= 2
            ],
        }
        adjustment = {
            **change,
            "reason": reason.strip(),
            "previous_plan_id": plan_id,
            "source": "user_requested_schedule_change",
            "other_sessions_unchanged": True,
        }
        draft = PlanningDraft(
            preview_id,
            self.athlete_id,
            "plan",
            1,
            "draft",
            _json_data(
                {
                    "goal_id": goal_id,
                    "plan": asdict(plan),
                    "summary": summary,
                    "adjustment_context": adjustment,
                    "planning_context": context,
                    "start_date": week_start,
                    "safety_day": today,
                    "safety_level": report.safety.level.value,
                    "safety_ruleset_version": report.safety.ruleset_version,
                    "data_revision": self.records.list_current_ids(self.athlete_id),
                    "data_gaps": report.data_gaps,
                    "confidence": report.confidence,
                    "block_cycle": prior.data.get("block_cycle"),
                    "block_week_index": prior.data.get("block_week_index"),
                    "plan_scope": "user-requested change to this seven-day plan only",
                }
            ),
        )
        self.drafts.create(draft)
        return {
            "preview_id": preview_id,
            "revision": 1,
            "plan": draft.data["plan"],
            "summary": summary,
            "adjustment_context": adjustment,
            "risk": asdict(report.safety),
            "data_gaps": report.data_gaps,
            "confidence": report.confidence,
            "requires_confirmation": True,
            "blocked": False,
        }

    def confirm_plan(self, preview_id: str, revision: int) -> dict[str, object]:
        draft = self._draft("plan", preview_id)
        if draft.revision != revision:
            raise ValueError("draft revision changed; review the latest draft")
        if draft.status == "confirmed":
            record = self._confirmed_record(draft)
            return {"plan": record.data, "record_id": record.record_id, "confirmed": True}
        today = self._now().astimezone(self.timezone).date()
        if str(draft.data["safety_day"]) != today.isoformat():
            raise ValueError("safety context is stale; preview the plan again")
        current_safety = (
            ReportService(
                self.athlete_id,
                self.records,
                timezone_name=self.timezone.key,
            )
            .daily(today)
            .safety
        )
        if current_safety.level.value != draft.data.get(
            "safety_level"
        ) or current_safety.ruleset_version != draft.data.get("safety_ruleset_version"):
            raise ValueError("safety context is stale; preview the plan again")
        expected_value = draft.data.get("data_revision")
        if not isinstance(expected_value, list):
            raise ValueError("plan preview has no data revision")
        expected_ids = tuple(str(item) for item in expected_value)
        if self.records.list_current_ids(self.athlete_id) != expected_ids:
            raise ValueError("source data changed; preview the plan again")
        goal_id = str(draft.data["goal_id"])
        if not any(goal.goal_id == goal_id for goal in self._goals()):
            raise ValueError("goal is no longer active; preview the plan again")
        stored = draft.data["plan"]
        if not isinstance(stored, dict):
            raise ValueError("invalid stored plan preview")
        plan = self._plan_from_data(stored)
        existing = self._plans_for_goal(goal_id)
        latest = (
            max(existing, key=lambda item: int(str(item.data.get("version", 0))))
            if existing
            else None
        )
        if plan.previous_plan_id != (str(latest.data["plan_id"]) if latest else None):
            raise ValueError("plan version changed; preview the plan again")
        approved = replace(
            plan,
            status="approved",
            sessions=tuple(session.transition("approved") for session in plan.sessions),
        )
        record = CanonicalRecord.create(
            athlete_id=self.athlete_id,
            kind="training-plan",
            occurred_at=self._now(),
            data=_json_data(
                {
                    **asdict(approved),
                    "summary": draft.data.get("summary"),
                    "intensity_guidance": draft.data.get("intensity_guidance"),
                    "planning_context": draft.data.get("planning_context"),
                    "goal_conflicts": draft.data.get("goal_conflicts"),
                    "replanning_context": draft.data.get("replanning_context"),
                    "adjustment_context": draft.data.get("adjustment_context"),
                    "start_date": draft.data.get("start_date"),
                    "block_cycle": draft.data.get("block_cycle"),
                    "block_week_index": draft.data.get("block_week_index"),
                }
            ),
        )
        saved = self.drafts.confirm(
            self.athlete_id,
            "plan",
            preview_id,
            revision,
            record,
            expected_current_ids=expected_ids,
        )
        return {"plan": saved.data, "record_id": saved.record_id, "confirmed": True}

    def preview_plan_update(
        self,
        *,
        plan_id: str,
        goal_id: str | None = None,
        start_date: date | None = None,
        requested_sessions: tuple[dict[str, object], ...] = (),
    ) -> dict[str, object]:
        prior = next(
            (
                item
                for item in self.records.list_current(self.athlete_id, kind="training-plan")
                if item.data.get("plan_id") == plan_id
            ),
            None,
        )
        if prior is None:
            raise KeyError("confirmed plan not found")
        execution_entries = [
            item for item in self.records.list_current(self.athlete_id)
            if item.kind.startswith(f"plan-execution:{plan_id}:")
        ]
        recent_pain = [
            item for item in execution_entries
            if isinstance(item.data.get("pain_after"), (int, float))
            and float(str(item.data["pain_after"])) >= 4
            and 0 <= (self._now() - item.occurred_at).days <= 7
        ]
        later_check_ins = self.records.list_current(self.athlete_id, kind="check-in")
        if any(
            not any(
                check.occurred_at > item.occurred_at
                and type(check.data.get("pain")) in {int, float}
                and float(str(check.data["pain"])) < 4
                and check.data.get("illness") is False
                for check in later_check_ins
            )
            for item in recent_pain
        ):
            return {
                "blocked": True,
                "reason": (
                    "recent execution feedback reported pain; "
                    "record a new check-in and reassess"
                ),
                "requires_confirmation": False,
            }
        context = prior.data.get("planning_context")
        if not isinstance(context, dict) or not isinstance(context.get("constraints"), dict):
            raise ValueError("previous plan has no reusable constraints")
        selected_goal_id = goal_id or str(prior.data.get("goal_id"))
        if self._goal_record(selected_goal_id) is None:
            raise ValueError("choose an active goal for replanning")
        today = self._now().astimezone(self.timezone).date()
        cycle = prior.data.get("block_cycle")
        if selected_goal_id != prior.data.get("goal_id") or not isinstance(cycle, dict):
            cycle = None
        if cycle is not None:
            if cycle.get("focus_source") == "confirmed_profile" and (
                current_training_preferences(self.records, self.athlete_id)["record_id"]
                != context.get("preference_record_id")
            ):
                raise ValueError("block preference changed; preview blocks again")
            anchor = date.fromisoformat(str(cycle["anchor_start_date"]))
            if start_date is None:
                previous_index = int(prior.data.get("block_week_index", 0))
                proposed_date = anchor + timedelta(days=(previous_index + 1) * 7)
                while proposed_date < today:
                    proposed_date += timedelta(days=7)
            else:
                proposed_date = start_date
            selected_focus = focus_for_date(cycle, proposed_date)
            raw = cycle.get("base_constraints")
        else:
            proposed_date = start_date or today
            selected_focus = None
            raw = context.get("requested_constraints", context["constraints"])
        if not isinstance(raw, dict):
            raise ValueError("previous plan has no reusable constraints")
        raw_availability = raw.get("available_minutes_by_weekday")
        if not isinstance(raw_availability, dict):
            raise ValueError("previous plan has no reusable constraints")
        constraints = TrainingConstraints(
            {int(key): int(value) for key, value in raw_availability.items()},
            frozenset(int(item) for item in raw.get("unavailable_weekdays", [])),
            int(raw["max_weekly_minutes"]),
            tuple(str(item) for item in raw["allowed_sports"]),
            preferred_quality_weekday=raw.get("preferred_quality_weekday"),
            preferred_long_weekday=raw.get("preferred_long_weekday"),
            focus_sport=raw.get("focus_sport"),
            aerobic_mixing=raw.get("aerobic_mixing"),
            use_saved_preferences=bool(raw.get("use_saved_preferences", True)),
            phase_ceiling_preapplied=bool(raw.get(
                "phase_ceiling_preapplied", raw.get("event_week_ceiling_preapplied", False)
            )),
        )
        execution_feedback = [
            {
                "record_id": item.record_id,
                "session_id": item.data.get("session_id"),
                "status": item.data.get("status"),
                "rpe": item.data.get("rpe"),
            }
            for item in execution_entries
        ]
        response_constraint = None
        if any(item["status"] in {"partial", "skipped"} for item in execution_feedback):
            response_constraint = (
                "Partial or skipped sessions are not carried forward. The next volume "
                "budget follows observed activity, current safety and the user's time limit."
            )
        if cycle is not None:
            constraints = replace(
                constraints,
                focus_sport=selected_focus,
                preferred_quality_weekday=cast(int | None, cycle.get("preferred_quality_weekday")),
                preferred_long_weekday=cast(int | None, cycle.get("preferred_long_weekday")),
            )
            forecast = cycle.get("forecast")
            if isinstance(forecast, list):
                week_index = (
                    proposed_date - date.fromisoformat(str(cycle["anchor_start_date"]))
                ).days // 7
                if 0 <= week_index < len(forecast) and isinstance(forecast[week_index], dict):
                    ceiling = forecast[week_index].get("weekly_ceiling_min")
                    if type(ceiling) is int and ceiling > 0:
                        constraints = replace(
                            constraints,
                            max_weekly_minutes=min(constraints.max_weekly_minutes, ceiling),
                            phase_ceiling_preapplied=(
                                forecast[week_index].get("phase")
                                in {"event_week_review", "taper_forecast"}
                                and ceiling <= constraints.max_weekly_minutes
                            ),
                        )
        preview = self.preview_plan(
            goal_id=selected_goal_id,
            start_date=proposed_date,
            constraints=constraints,
            requested_sessions=requested_sessions,
        )
        if preview.get("blocked"):
            return preview
        prior_summary = prior.data.get("summary")
        next_summary = preview["summary"]
        prior_minutes = (
            int(prior_summary.get("total_minutes", 0)) if isinstance(prior_summary, dict) else None
        )
        new_minutes = int(next_summary["total_minutes"]) if isinstance(next_summary, dict) else None
        comparison = {
            "previous_plan_id": plan_id,
            "previous_goal_id": prior.data.get("goal_id"),
            "new_goal_id": selected_goal_id,
            "minutes_change": new_minutes - prior_minutes
            if prior_minutes is not None and new_minutes is not None
            else None,
            "reason": self.review_plan(plan_id)["reasons"],
            "execution_feedback": execution_feedback,
            "response_constraint": response_constraint,
            "block_focus_change": {
                "from": prior.data.get("planning_context", {})
                .get("constraints", {})
                .get("focus_sport"),
                "to": selected_focus,
            }
            if cycle is not None
            else None,
        }
        draft = self._draft("plan", str(preview["preview_id"]))
        block_week_index = (
            (proposed_date - date.fromisoformat(str(cycle["anchor_start_date"]))).days // 7
            if cycle is not None
            else None
        )
        updated = PlanningDraft(
            draft.draft_id,
            self.athlete_id,
            "plan",
            draft.revision + 1,
            "draft",
            _json_data(
                {
                    **draft.data,
                    "replanning_context": comparison,
                    "block_cycle": cycle,
                    "block_week_index": block_week_index,
                    "plan_scope": (
                        "current week is a confirmable draft; later weeks are forecasts only"
                        if cycle is not None
                        else draft.data.get("plan_scope")
                    ),
                }
            ),
        )
        self.drafts.revise(updated, expected_revision=draft.revision)
        return {
            **preview,
            "revision": updated.revision,
            "replanning_context": comparison,
            "block_cycle": cycle,
            "block_week_index": block_week_index,
            "plan_scope": updated.data["plan_scope"],
        }

    def review_plan(self, plan_id: str) -> dict[str, object]:
        record = next(
            (
                item
                for item in self.records.list_current(self.athlete_id, kind="training-plan")
                if item.data.get("plan_id") == plan_id
            ),
            None,
        )
        if record is None:
            raise KeyError("confirmed plan not found")
        context = record.data.get("planning_context")
        reasons: list[str] = []
        active_goals = self.records.list_current(self.athlete_id, kind="goal")
        current_goal_ids = {str(item.data.get("goal_id")) for item in active_goals}
        if str(record.data.get("goal_id")) not in current_goal_ids:
            reasons.append("plan goal was replaced or deleted")
        if not isinstance(context, dict):
            reasons.append("plan lacks a goal and data revision snapshot")
        else:
            prior_goal_ids = {str(item) for item in context.get("active_goal_record_ids", [])}
            if prior_goal_ids != {item.record_id for item in active_goals}:
                reasons.append("active goals changed")
            if context.get("preference_source") == "confirmed_profile" or (
                isinstance(context.get("requested_constraints"), dict)
                and context["requested_constraints"].get("use_saved_preferences", True)
            ):
                current_preference = current_training_preferences(self.records, self.athlete_id)
                if context.get("preference_record_id") != current_preference["record_id"]:
                    reasons.append("confirmed training preferences changed")
            if context.get("goal_record_id") != (
                self._goal_record(str(record.data.get("goal_id"))).record_id
                if self._goal_record(str(record.data.get("goal_id")))
                else None
            ):
                reasons.append("primary goal changed")
        new_evidence = [
            item
            for item in self.records.list_current(self.athlete_id)
            if item.kind in {"activity", "check-in"} and item.created_at > record.created_at
        ]
        if new_evidence:
            reasons.append("new training or check-in data arrived")
        confirmed_executions = [
            item for item in self.records.list_current(self.athlete_id)
            if item.kind.startswith(f"plan-execution:{plan_id}:")
        ]
        if confirmed_executions:
            reasons.append("athlete-confirmed session execution feedback is available")
        linked_activity_ids = {
            str(item.data["activity_id"])
            for item in confirmed_executions if item.data.get("activity_id")
        }
        planned_by_day: dict[str, list[dict[str, object]]] = {}
        sessions_value = record.data.get("sessions")
        for session in sessions_value if isinstance(sessions_value, list) else []:
            if isinstance(session, dict):
                planned_by_day.setdefault(str(session.get("session_date")), []).append(session)
        plan_start_text = record.data.get("start_date") or (
            min(planned_by_day) if planned_by_day else None
        )
        plan_start = (
            date.fromisoformat(str(plan_start_text)) if plan_start_text is not None else None
        )
        activities_by_day: dict[str, list[CanonicalRecord]] = {}
        for item in new_evidence:
            if item.kind == "activity":
                day = item.occurred_at.astimezone(self.timezone).date().isoformat()
                if plan_start is not None and 0 <= (date.fromisoformat(day) - plan_start).days < 7:
                    activities_by_day.setdefault(day, []).append(item)
        possible_activity_changes: list[dict[str, object]] = []
        ambiguous_activity_days: list[str] = []
        for day, activities in sorted(activities_by_day.items()):
            activities = [item for item in activities if item.record_id not in linked_activity_ids]
            if not activities:
                continue
            planned = planned_by_day.get(day, [])
            if len(activities) != 1 or len(planned) > 1:
                ambiguous_activity_days.append(day)
                continue
            activity = activities[0]
            actual_sport = normalize_sport(activity.data.get("sport"))
            try:
                actual_minutes = float(str(activity.data.get("duration_min")))
            except (TypeError, ValueError):
                actual_minutes = None
            if actual_minutes is not None and (
                not math.isfinite(actual_minutes) or actual_minutes <= 0
            ):
                actual_minutes = None
            expected = planned[0] if planned else None
            expected_minutes = (
                int(str(expected.get("duration_min", 0))) if expected is not None else None
            )
            if expected is None or (
                actual_sport != normalize_sport(expected.get("sport"))
                or (
                    actual_minutes is not None
                    and expected_minutes is not None
                    and abs(actual_minutes - expected_minutes) >= max(10, expected_minutes * 0.2)
                )
            ):
                possible_activity_changes.append(
                    {
                        "day": day,
                        "activity_record_id": activity.record_id,
                        "planned_session_id": expected.get("session_id") if expected else None,
                        "planned_sport": expected.get("sport") if expected else None,
                        "actual_sport": actual_sport,
                        "planned_minutes": expected_minutes,
                        "actual_minutes": actual_minutes,
                        "interpretation": (
                            "possible deviation; confirm with athlete before treating "
                            "as a changed session"
                        ),
                    }
                )
        if possible_activity_changes:
            reasons.append("possible plan and activity differences need review")
        cycle = record.data.get("block_cycle")
        if isinstance(cycle, dict):
            try:
                anchor = date.fromisoformat(str(cycle["anchor_start_date"]))
                following = anchor + timedelta(
                    days=(int(record.data.get("block_week_index", 0)) + 1) * 7
                )
                if self._now().astimezone(self.timezone).date() >= following:
                    reasons.append("next focus block is due")
            except (ValueError, TypeError):
                reasons.append("block cycle needs review")
        goal = self._goal_record(str(record.data.get("goal_id")))
        if goal and goal.data.get("deadline"):
            try:
                if (date.fromisoformat(str(goal.data["deadline"])) - self._now().date()).days <= 7:
                    reasons.append("goal deadline is within seven days")
            except ValueError:
                pass
        return {
            "plan_id": plan_id,
            "goal_id": record.data.get("goal_id"),
            "review_required": bool(reasons),
            "reasons": reasons,
            "possible_activity_changes": possible_activity_changes,
            "confirmed_executions": [
                {
                    "session_id": item.data.get("session_id"),
                    "activity_id": item.data.get("activity_id"),
                    "status": item.data.get("status"),
                    "rpe": item.data.get("rpe"),
                    "pain_after": item.data.get("pain_after"),
                }
                for item in confirmed_executions
            ],
            "ambiguous_activity_days": ambiguous_activity_days,
            "suggested_active_goal_ids": sorted(current_goal_ids),
            "next_step": "preview and confirm a revised plan before replacing the schedule"
            if reasons
            else "continue and review after new evidence",
            "suggested_action": (
                "confirm possible activity differences, then preview remaining-session changes"
                if possible_activity_changes
                else "review goal, readiness and upcoming schedule before a plan preview"
                if reasons
                else None
            ),
            "automatic_change_applied": False,
        }

    def _goal_view(self, draft: PlanningDraft) -> dict[str, object]:
        data = draft.data
        missing = []
        if not data.get("goal_type"):
            missing.append("goal_type")
        if not data.get("title"):
            missing.append("title")
        explicit = data.get("explicit_fields")
        explicit_set = {str(item) for item in explicit} if isinstance(explicit, list) else set()
        assumptions = []
        if "title" not in explicit_set:
            assumptions.append("title uses the user's original idea")
        if "priority" not in explicit_set:
            assumptions.append("priority defaults to 3 of 5 until the user changes it")
        conflicts: list[dict[str, object]] = []
        if data.get("goal_type") and data.get("title"):
            candidate = Goal(
                draft.draft_id,
                self.athlete_id,
                cast(GoalType, str(data["goal_type"])),
                str(data["title"]),
                int(str(data["priority"])),
                date.fromisoformat(str(data["deadline"])) if data.get("deadline") else None,
            )
            conflicts = [
                asdict(item)
                for item in detect_conflicts([*self._goals(), candidate])
                if draft.draft_id in item.goal_ids
            ]
        return {
            "proposal_id": draft.draft_id,
            "revision": draft.revision,
            "status": draft.status,
            "idea": data["idea"],
            "proposed_goal": {
                key: data.get(key)
                for key in (
                    "goal_type",
                    "title",
                    "priority",
                    "deadline",
                    "target_metric",
                    "target_value",
                )
            },
            "missing_fields": missing,
            "assumptions": assumptions,
            "conflicts": conflicts,
            "requires_confirmation": True,
        }

    def _draft(self, kind: str, draft_id: str) -> PlanningDraft:
        draft = self.drafts.get(self.athlete_id, kind, draft_id)
        if draft is None:
            raise KeyError("draft not found")
        return draft

    def _confirmed_record(self, draft: PlanningDraft) -> CanonicalRecord:
        if not draft.confirmed_record_id:
            raise RuntimeError("confirmed draft has no record")
        record = self.records.get(draft.confirmed_record_id)
        if record is None or record.athlete_id != self.athlete_id:
            raise RuntimeError("confirmed record is missing")
        return record

    def _goals(self) -> list[Goal]:
        goals = []
        for record in self.records.list_current(self.athlete_id, kind="goal"):
            data = record.data
            if str(data.get("status", "active")) != "active":
                continue
            goals.append(
                Goal(
                    str(data["goal_id"]),
                    self.athlete_id,
                    cast(GoalType, str(data["goal_type"])),
                    str(data["title"]),
                    int(str(data["priority"])),
                    date.fromisoformat(str(data["deadline"])) if data.get("deadline") else None,
                    str(data["target_metric"]) if data.get("target_metric") else None,
                    float(str(data["target_value"]))
                    if data.get("target_value") is not None
                    else None,
                )
            )
        return goals

    def _plans_for_goal(self, goal_id: str) -> list[CanonicalRecord]:
        return [
            item
            for item in self.records.list_current(self.athlete_id, kind="training-plan")
            if str(item.data.get("goal_id")) == goal_id
        ]

    @staticmethod
    def _plan_from_data(data: dict[str, Any]) -> TrainingPlan:
        sessions_value = data.get("sessions")
        if not isinstance(sessions_value, list):
            raise ValueError("invalid stored plan sessions")
        sessions = tuple(
            PlannedSession(
                session_id=str(item["session_id"]),
                session_date=date.fromisoformat(str(item["session_date"])),
                sport=str(item["sport"]),
                duration_min=int(item["duration_min"]),
                intensity=str(item["intensity"]),
                objective=str(item["objective"]),
                expected_load=float(item["expected_load"]),
                session_type=str(item.get("session_type", "aerobic")),
                distance_m=int(item["distance_m"]) if item.get("distance_m") is not None else None,
                session_order=int(item.get("session_order", 1)),
                brick_id=str(item["brick_id"]) if item.get("brick_id") else None,
            )
            for item in sessions_value
        )
        return TrainingPlan(
            plan_id=str(data["plan_id"]),
            athlete_id=str(data["athlete_id"]),
            goal_id=str(data["goal_id"]),
            version=int(data["version"]),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            sessions=sessions,
            status=str(data.get("status", "draft")),
            change_reason=str(data.get("change_reason", "initial plan")),
            previous_plan_id=str(data["previous_plan_id"])
            if data.get("previous_plan_id")
            else None,
        )
