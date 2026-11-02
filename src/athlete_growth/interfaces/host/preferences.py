"""Host-neutral access to confirmed private training preferences."""

from __future__ import annotations

from pathlib import Path

from athlete_growth.application.preferences import TrainingPreferenceService
from athlete_growth.interfaces.host.tools import HostRuntime


def _service(data_home: str | Path) -> TrainingPreferenceService:
    runtime = HostRuntime.load(data_home)
    return TrainingPreferenceService(runtime.athlete_id, runtime.records)


def get_training_preferences(data_home: str | Path) -> dict[str, object]:
    return _service(data_home).get()


def preview_training_preferences(
    data_home: str | Path,
    changes: dict[str, object],
) -> dict[str, object]:
    return _service(data_home).preview(changes)


def confirm_training_preferences(
    data_home: str | Path,
    changes: dict[str, object],
    preview_hash: str,
) -> dict[str, object]:
    return _service(data_home).confirm(changes, preview_hash)
