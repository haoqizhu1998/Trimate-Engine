"""Host-neutral planned-session execution feedback."""

from __future__ import annotations

from pathlib import Path

from athlete_growth.application.plan.execution import PlanExecutionService
from athlete_growth.interfaces.host.tools import HostRuntime


def _service(data_home: str | Path) -> PlanExecutionService:
    runtime = HostRuntime.load(data_home)
    profile = runtime.profiles.get_by_id(runtime.athlete_id)
    if profile is None:
        raise RuntimeError("default profile record is missing")
    return PlanExecutionService(
        runtime.athlete_id, runtime.records, timezone_name=profile.timezone_name
    )


def list_plan_execution(data_home: str | Path, plan_id: str) -> dict[str, object]:
    return _service(data_home).list(plan_id)


def preview_plan_execution(
    data_home: str | Path,
    *,
    plan_id: str,
    session_id: str,
    status: str,
    activity_id: str | None = None,
    rpe: int | None = None,
    pain_after: int | None = None,
    reason: str | None = None,
) -> dict[str, object]:
    return _service(data_home).preview(
        plan_id=plan_id, session_id=session_id, status=status, activity_id=activity_id,
        rpe=rpe, pain_after=pain_after, reason=reason,
    )


def confirm_plan_execution(
    data_home: str | Path,
    *,
    plan_id: str,
    session_id: str,
    status: str,
    preview_hash: str,
    activity_id: str | None = None,
    rpe: int | None = None,
    pain_after: int | None = None,
    reason: str | None = None,
) -> dict[str, object]:
    return _service(data_home).confirm(
        plan_id=plan_id, session_id=session_id, status=status, preview_hash=preview_hash,
        activity_id=activity_id, rpe=rpe, pain_after=pain_after, reason=reason,
    )
