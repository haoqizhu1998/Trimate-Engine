"""Evidence-bound daily decisions and user-approved personal adaptation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from athlete_growth.adapters.storage.sqlite_growth import SQLiteGrowthRepository
from athlete_growth.application.preferences import current_training_preferences
from athlete_growth.application.report import ReportService
from athlete_growth.domain.safety import RiskLevel
from athlete_growth.ports.repositories import CanonicalRecordRepository

DECISION_VERSION = "training-decision/1.0"
ADAPTATION_VERSION = "growth-adaptation/1.0"
_OPTION_LABELS = {
    "usual": "Stay within the usual routine or confirmed plan; do not add load.",
    "easy": "Use an easier or shorter session within current constraints.",
    "pause": "Pause training and reassess; seek appropriate help for concerning symptoms.",
}


def _digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


class GrowthDecisionService:
    def __init__(
        self,
        athlete_id: str,
        records: CanonicalRecordRepository,
        growth: SQLiteGrowthRepository,
        *,
        timezone_name: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.athlete_id = athlete_id
        self.records = records
        self.growth = growth
        self.timezone = ZoneInfo(timezone_name)
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must be timezone-aware")
        return now.astimezone(UTC)

    def _preview_state(self) -> tuple[dict[str, object], tuple[str, ...], str | None]:
        now = self._now()
        day = now.astimezone(self.timezone).date()
        before = self.records.list_current_ids(self.athlete_id)
        visible_ids = tuple(
            record.record_id for record in self.records.list_current(self.athlete_id, end=now)
        )
        report = ReportService(
            self.athlete_id,
            self.records,
            timezone_name=self.timezone.key,
            projection_record_ids=visible_ids,
        ).daily(day)
        after = self.records.list_current_ids(self.athlete_id)
        if before != after:
            raise ValueError("source data changed; preview the decision again")
        current = self.growth.current_decision(self.athlete_id, day.isoformat())
        current_id = str(current["decision_id"]) if current is not None else None
        active = self.growth.active_adaptation(self.athlete_id)
        if report.safety.level == RiskLevel.HIGH:
            options = ("pause",)
            recommended = "pause"
        elif report.safety.level in {RiskLevel.MODERATE, RiskLevel.UNKNOWN}:
            options = ("easy", "pause")
            recommended = "easy"
        else:
            options = ("usual", "easy", "pause")
            recommended = "easy" if active is not None or report.confidence < 0.5 else "usual"
        context: dict[str, object] = {
            "day": day.isoformat(),
            "risk_level": report.safety.level.value,
            "risk_reasons": list(report.safety.reasons),
            "safety_actions": list(report.safety.actions),
            "hrv_context": dict(report.hrv_context or {}),
            "data_gaps": list(report.data_gaps),
            "confidence": report.confidence,
            "evidence_ids": list(
                dict.fromkeys((*report.evidence_ids, *report.safety.evidence_ids))
            ),
            "report_algorithm_version": report.algorithm_version,
            "safety_ruleset_version": report.safety.ruleset_version,
            "active_adaptation_id": active["adaptation_id"] if active else None,
            "decision_version": DECISION_VERSION,
        }
        preview_hash = _digest(
            {
                "athlete_id": self.athlete_id,
                "report": asdict(report),
                "source_record_ids": before,
                "current_decision_id": current_id,
                "adaptation_id": context["active_adaptation_id"],
                "options": options,
                "recommended": recommended,
                "decision_version": DECISION_VERSION,
            }
        )
        view = {
            **context,
            "options": [
                {"id": option, "description": _OPTION_LABELS[option]} for option in options
            ],
            "recommended_option": recommended,
            "recommendation_basis": (
                "safety override"
                if report.safety.level != RiskLevel.LOW
                else "user-approved adaptation"
                if active is not None
                else "limited data"
                if report.confidence < 0.5
                else "current low-risk report"
            ),
            "current_decision_id": current_id,
            "preview_hash": preview_hash,
            "requires_confirmation": True,
            "saved": False,
        }
        return view, before, current_id

    def preview_decision(self) -> dict[str, object]:
        return self._preview_state()[0]

    def confirm_decision(self, preview_hash: str, selected_option: str) -> dict[str, object]:
        decision_id = _digest(
            {
                "athlete_id": self.athlete_id,
                "preview_hash": preview_hash,
                "selected": selected_option,
            }
        )
        previous = self.growth.get_decision(self.athlete_id, decision_id)
        if previous is not None:
            if not previous["is_current"]:
                raise ValueError("decision was superseded; preview it again")
            return self._public_decision(previous)
        view, source_ids, current_id = self._preview_state()
        if preview_hash != view["preview_hash"]:
            raise ValueError("decision context changed; preview it again")
        options = cast(list[dict[str, object]], view["options"])
        allowed = {str(item["id"]) for item in options}
        if selected_option not in allowed:
            raise ValueError("selected option is not allowed by the current safety result")
        context = {
            key: value
            for key, value in view.items()
            if key not in {"options", "preview_hash", "requires_confirmation", "saved"}
        }
        saved = self.growth.confirm_decision(
            athlete_id=self.athlete_id,
            decision_id=decision_id,
            day=str(view["day"]),
            preview_hash=preview_hash,
            selected_option=selected_option,
            recommended_option=str(view["recommended_option"]),
            context=context,
            expected_current_id=current_id,
            expected_adaptation_id=cast(str | None, view["active_adaptation_id"]),
            expected_record_ids=source_ids,
            now=self._now(),
        )
        return self._public_decision(saved)

    @staticmethod
    def _public_decision(value: dict[str, object]) -> dict[str, object]:
        return {
            key: item for key, item in value.items() if key not in {"athlete_id", "preview_hash"}
        }

    def list_decisions(self, limit: int = 10) -> dict[str, object]:
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        result = []
        for decision in self.growth.list_decisions(self.athlete_id, limit):
            public = self._public_decision(decision)
            public["objective_activity_observed"] = self._activity_observed(str(decision["day"]))
            public["feedback"] = self.growth.feedback(self.athlete_id, str(decision["decision_id"]))
            if isinstance(public["feedback"], dict):
                public["feedback"].pop("athlete_id", None)
            result.append(public)
        return {"decisions": result}

    def _activity_observed(self, day_text: str) -> bool:
        day = date.fromisoformat(day_text)
        start = datetime.combine(day, time.min, tzinfo=self.timezone).astimezone(UTC)
        end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=self.timezone)
        end = end.astimezone(UTC) - timedelta(microseconds=1)
        return bool(
            self.records.list_current(self.athlete_id, kind="activity", start=start, end=end)
        )

    def _activity_days(self, now: datetime) -> set[str]:
        activities = self.records.list_current(
            self.athlete_id,
            kind="activity",
            start=now - timedelta(days=90),
            end=now,
        )
        return {
            activity.occurred_at.astimezone(self.timezone).date().isoformat()
            for activity in activities
        }

    def record_outcome(
        self,
        *,
        decision_id: str,
        expected_revision: int,
        completed: bool,
        effect: str,
        pain_after: int | None = None,
        note: str = "",
    ) -> dict[str, object]:
        if expected_revision < 0 or effect not in {"better", "same", "worse"}:
            raise ValueError("invalid outcome revision or effect")
        if pain_after is not None and not 0 <= pain_after <= 10:
            raise ValueError("pain_after must be between 0 and 10")
        if len(note) > 300:
            raise ValueError("note is too long")
        saved = self.growth.save_feedback(
            athlete_id=self.athlete_id,
            decision_id=decision_id,
            expected_revision=expected_revision,
            completed=completed,
            effect=effect,
            pain_after=pain_after,
            note=note,
            now=self._now(),
        )
        saved.pop("athlete_id", None)
        return {"feedback": saved, "growth_profile": self.growth_profile()}

    def growth_profile(self) -> dict[str, object]:
        now = self._now()
        goals = self.records.list_current(self.athlete_id, kind="goal")
        plans = self.records.list_current(self.athlete_id, kind="training-plan")
        latest_plan = max(plans, key=lambda item: item.occurred_at) if plans else None
        outcomes = self.growth.recent_outcomes(self.athlete_id, now - timedelta(days=90))
        active = self.growth.active_adaptation(self.athlete_id)
        completed = [item for item in outcomes if bool(item["completed"])]
        low_risk = [item for item in completed if item["risk_level"] == "low"]
        activity_days = self._activity_days(now)
        objective_supported = sum(item["day"] in activity_days for item in completed)
        usual = [item for item in low_risk if item["selected_option"] == "usual"]
        easy = [item for item in low_risk if item["selected_option"] == "easy"]
        candidate_objective_supported = sum(item["day"] in activity_days for item in usual + easy)
        usual_worse = [
            item
            for item in usual
            if item["effect"] == "worse"
            or (isinstance(item["pain_after"], int) and item["pain_after"] >= 4)
        ]
        easy_not_worse = [
            item
            for item in easy
            if item["effect"] in {"better", "same"}
            and (
                item["pain_after"] is None
                or (isinstance(item["pain_after"], int) and item["pain_after"] < 4)
            )
        ]
        signature = self.growth.source_signature(self.athlete_id, now)
        candidate: dict[str, object] | None = None
        if (
            active is None
            and len(usual) >= 3
            and len(easy) >= 3
            and len(usual_worse) >= 2
            and len(easy_not_worse) >= 2
        ):
            evidence = [str(item["decision_id"]) for item in usual + easy]
            candidate = {
                "adaptation_id": _digest(
                    (self.athlete_id, ADAPTATION_VERSION, signature, "prefer_easy_when_low_risk")
                ),
                "preferred_option": "easy",
                "scope": "low-risk daily decision recommendation only",
                "reason": "Repeated user-reported outcomes favor trying an easier option.",
                "evidence_decision_ids": evidence,
                "usual_samples": len(usual),
                "usual_worse_samples": len(usual_worse),
                "easy_samples": len(easy),
                "easy_not_worse_samples": len(easy_not_worse),
                "objective_activity_days": candidate_objective_supported,
                "source_signature": signature,
                "limitations": (
                    "Observational association; not proof of causal benefit. "
                    + (
                        "No matching activity record; based on user reports only."
                        if candidate_objective_supported == 0
                        else "Activity matches do not prove the chosen session was completed."
                    )
                ),
                "requires_confirmation": True,
            }
        return {
            "window_days": 90,
            "training_preferences": current_training_preferences(self.records, self.athlete_id),
            "confirmed_goals": [
                {
                    "goal_id": goal.data.get("goal_id"),
                    "title": goal.data.get("title"),
                    "goal_type": goal.data.get("goal_type"),
                    "deadline": goal.data.get("deadline"),
                    "evidence_record_id": goal.record_id,
                }
                for goal in goals[-10:]
                if goal.data.get("status", "active") == "active"
            ],
            "saved_plan_count": len(plans),
            "latest_plan": (
                {
                    "plan_id": latest_plan.data.get("plan_id"),
                    "version": latest_plan.data.get("version"),
                    "evidence_record_id": latest_plan.record_id,
                }
                if latest_plan is not None
                else None
            ),
            "outcome_count": len(outcomes),
            "completed_count": len(completed),
            "objective_activity_days": objective_supported,
            "usual_samples": len(usual),
            "easy_samples": len(easy),
            "active_adaptation": active,
            "candidate_adaptation": candidate,
            "learning_status": (
                "active"
                if active is not None
                else "candidate"
                if candidate
                else "insufficient_or_no_new_evidence"
            ),
            "safety_rules_adaptable": False,
            "plan_changed": False,
        }

    def confirm_adaptation(self, adaptation_id: str) -> dict[str, object]:
        profile = self.growth_profile()
        active = profile["active_adaptation"]
        if isinstance(active, dict) and active["adaptation_id"] == adaptation_id:
            return {"active_adaptation": active, "plan_changed": False}
        candidate = profile["candidate_adaptation"]
        if not isinstance(candidate, dict) or candidate["adaptation_id"] != adaptation_id:
            raise ValueError("adaptation changed or is unavailable; review the profile again")
        saved = self.growth.accept_adaptation(
            athlete_id=self.athlete_id,
            adaptation_id=adaptation_id,
            source_signature=str(candidate["source_signature"]),
            evidence_decision_ids=[str(value) for value in candidate["evidence_decision_ids"]],
            now=self._now(),
        )
        return {"active_adaptation": saved, "plan_changed": False}

    def revoke_adaptation(self) -> dict[str, object]:
        revoked = self.growth.revoke_adaptation(self.athlete_id, self._now())
        return {"revoked": revoked, "active_adaptation": None, "plan_changed": False}
