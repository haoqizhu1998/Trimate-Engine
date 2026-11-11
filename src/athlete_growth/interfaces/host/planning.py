"""Host-neutral entry points for conversational goal and plan workflows."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from athlete_growth.adapters.storage import (
    ProfileRegistry,
    SQLiteAthleteProfileRepository,
    SQLitePlanningDraftRepository,
    SQLiteRecordStore,
    VaultLayout,
)
from athlete_growth.application.plan.workflow import PlanningWorkflow
from athlete_growth.domain.training import TrainingConstraints


def _workflow(data_home: str | Path) -> PlanningWorkflow:
    home = Path(data_home).expanduser().resolve()
    athlete_id = ProfileRegistry(home / "config.toml").default_profile_id()
    if athlete_id is None:
        raise RuntimeError("data home is not initialized")
    database_file = VaultLayout(home, athlete_id).database_file
    if not database_file.is_file():
        raise RuntimeError("default profile database is missing")
    profile = SQLiteAthleteProfileRepository(database_file).get_by_id(athlete_id)
    if profile is None:
        raise RuntimeError("default profile record is missing")
    records = SQLiteRecordStore(database_file)
    drafts = SQLitePlanningDraftRepository(database_file, records)
    return PlanningWorkflow(
        athlete_id,
        records,
        drafts,
        timezone_name=profile.timezone_name,
    )


def list_open_drafts(data_home: str | Path) -> dict[str, object]:
    return _workflow(data_home).list_open_drafts()


def propose_goal(
    data_home: str | Path,
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
    return _workflow(data_home).propose_goal(
        idea=idea,
        proposal_id=proposal_id,
        expected_revision=expected_revision,
        goal_type=goal_type,
        title=title,
        priority=priority,
        deadline=deadline,
        target_metric=target_metric,
        target_value=target_value,
    )


def confirm_goal(data_home: str | Path, proposal_id: str, revision: int) -> dict[str, object]:
    return _workflow(data_home).confirm_goal(proposal_id, revision)


def preview_goal_change(
    data_home: str | Path,
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
    return _workflow(data_home).preview_goal_change(
        action=action,
        goal_id=goal_id,
        reason=reason,
        goal_type=goal_type,
        title=title,
        priority=priority,
        deadline=deadline,
        target_metric=target_metric,
        target_value=target_value,
    )


def confirm_goal_change(data_home: str | Path, preview_id: str, revision: int) -> dict[str, object]:
    return _workflow(data_home).confirm_goal_change(preview_id, revision)


def review_plan(data_home: str | Path, plan_id: str) -> dict[str, object]:
    return _workflow(data_home).review_plan(plan_id)


def preview_plan_update(
    data_home: str | Path,
    *,
    plan_id: str,
    goal_id: str | None = None,
    start_date: date | None = None,
    requested_sessions: tuple[dict[str, object], ...] = (),
) -> dict[str, object]:
    return _workflow(data_home).preview_plan_update(
        plan_id=plan_id,
        goal_id=goal_id,
        start_date=start_date,
        requested_sessions=requested_sessions,
    )


def preview_plan_adjustment(
    data_home: str | Path,
    *,
    plan_id: str,
    session_id: str,
    action: str,
    reason: str,
    new_date: date | None = None,
    new_sport: str | None = None,
    new_duration_min: int | None = None,
) -> dict[str, object]:
    return _workflow(data_home).preview_plan_adjustment(
        plan_id=plan_id,
        session_id=session_id,
        action=action,
        reason=reason,
        new_date=new_date,
        new_sport=new_sport,
        new_duration_min=new_duration_min,
    )


def preview_plan(
    data_home: str | Path,
    *,
    goal_id: str,
    start_date: date,
    available_minutes_by_weekday: dict[int, int],
    max_weekly_minutes: int,
    sport: str | None = None,
    sports: tuple[str, ...] = (),
    unavailable_weekdays: tuple[int, ...] = (),
    preferred_quality_weekday: int | None = None,
    preferred_long_weekday: int | None = None,
    focus_sport: str | None = None,
    aerobic_mixing: bool | None = None,
    use_saved_preferences: bool = True,
    requested_sessions: tuple[dict[str, object], ...] = (),
) -> dict[str, object]:
    if (sport is None) == (not sports):
        raise ValueError("provide exactly one sport or sports list")
    selected_sports = sports if sport is None else (sport,)
    if any(item not in {"running", "cycling", "swimming"} for item in selected_sports):
        raise ValueError("plan sports must be running, cycling or swimming")
    if len(set(selected_sports)) != len(selected_sports):
        raise ValueError("plan sports must not repeat")
    constraints = TrainingConstraints(
        available_minutes_by_weekday=available_minutes_by_weekday,
        unavailable_weekdays=frozenset(unavailable_weekdays),
        max_weekly_minutes=max_weekly_minutes,
        allowed_sports=selected_sports,
        preferred_quality_weekday=preferred_quality_weekday,
        preferred_long_weekday=preferred_long_weekday,
        focus_sport=focus_sport,
        aerobic_mixing=aerobic_mixing,
        use_saved_preferences=use_saved_preferences,
    )
    return _workflow(data_home).preview_plan(
        goal_id=goal_id,
        start_date=start_date,
        constraints=constraints,
        requested_sessions=requested_sessions,
    )


def preview_plan_blocks(
    data_home: str | Path,
    *,
    goal_id: str,
    start_date: date,
    available_minutes_by_weekday: dict[int, int],
    max_weekly_minutes: int,
    weeks: int,
    sport: str | None = None,
    sports: tuple[str, ...] = (),
    focus_sequence: tuple[str, ...] = (),
    unavailable_weekdays: tuple[int, ...] = (),
    preferred_quality_weekday: int | None = None,
    preferred_long_weekday: int | None = None,
    focus_sport: str | None = None,
    aerobic_mixing: bool | None = None,
    use_saved_preferences: bool = True,
    requested_sessions: tuple[dict[str, object], ...] = (),
) -> dict[str, object]:
    if (sport is None) == (not sports):
        raise ValueError("provide exactly one sport or sports list")
    selected_sports = sports if sport is None else (sport,)
    if any(item not in {"running", "cycling", "swimming"} for item in selected_sports):
        raise ValueError("plan sports must be running, cycling or swimming")
    if len(set(selected_sports)) != len(selected_sports):
        raise ValueError("plan sports must not repeat")
    constraints = TrainingConstraints(
        available_minutes_by_weekday=available_minutes_by_weekday,
        unavailable_weekdays=frozenset(unavailable_weekdays),
        max_weekly_minutes=max_weekly_minutes,
        allowed_sports=selected_sports,
        preferred_quality_weekday=preferred_quality_weekday,
        preferred_long_weekday=preferred_long_weekday,
        focus_sport=focus_sport,
        aerobic_mixing=aerobic_mixing,
        use_saved_preferences=use_saved_preferences,
    )
    return _workflow(data_home).preview_plan_blocks(
        goal_id=goal_id,
        start_date=start_date,
        constraints=constraints,
        weeks=weeks,
        focus_sequence=focus_sequence,
        requested_sessions=requested_sessions,
    )


def confirm_plan(data_home: str | Path, preview_id: str, revision: int) -> dict[str, object]:
    return _workflow(data_home).confirm_plan(preview_id, revision)
