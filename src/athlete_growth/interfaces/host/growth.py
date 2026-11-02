"""Host-neutral entry points for the private growth and decision loop."""

from __future__ import annotations

from pathlib import Path

from athlete_growth.adapters.storage.sqlite_growth import SQLiteGrowthRepository
from athlete_growth.application.learn.decision import GrowthDecisionService
from athlete_growth.interfaces.host.tools import HostRuntime


def _service(data_home: str | Path) -> GrowthDecisionService:
    runtime = HostRuntime.load(data_home)
    profile = runtime.profiles.get_by_id(runtime.athlete_id)
    if profile is None:
        raise RuntimeError("default profile record is missing")
    return GrowthDecisionService(
        runtime.athlete_id,
        runtime.records,
        SQLiteGrowthRepository(runtime.layout.database_file, runtime.records),
        timezone_name=profile.timezone_name,
    )


def get_growth_profile(data_home: str | Path) -> dict[str, object]:
    return _service(data_home).growth_profile()


def preview_training_decision(data_home: str | Path) -> dict[str, object]:
    return _service(data_home).preview_decision()


def confirm_training_decision(
    data_home: str | Path, preview_hash: str, selected_option: str
) -> dict[str, object]:
    return _service(data_home).confirm_decision(preview_hash, selected_option)


def list_training_decisions(data_home: str | Path, limit: int = 10) -> dict[str, object]:
    return _service(data_home).list_decisions(limit)


def record_decision_outcome(
    data_home: str | Path,
    *,
    decision_id: str,
    expected_revision: int,
    completed: bool,
    effect: str,
    pain_after: int | None = None,
    note: str = "",
) -> dict[str, object]:
    return _service(data_home).record_outcome(
        decision_id=decision_id,
        expected_revision=expected_revision,
        completed=completed,
        effect=effect,
        pain_after=pain_after,
        note=note,
    )


def confirm_growth_adaptation(data_home: str | Path, adaptation_id: str) -> dict[str, object]:
    return _service(data_home).confirm_adaptation(adaptation_id)


def revoke_growth_adaptation(data_home: str | Path) -> dict[str, object]:
    return _service(data_home).revoke_adaptation()
