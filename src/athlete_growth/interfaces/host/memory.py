"""Explicit, profile-bound manual training memory tools."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from athlete_growth.application.memory import TrainingMemoryService
from athlete_growth.interfaces.host.tools import HostRuntime


def _service(data_home: str | Path) -> TrainingMemoryService:
    runtime = HostRuntime.load(data_home)
    return TrainingMemoryService(runtime.athlete_id, runtime.records)


def preview_training_memory(
    data_home: str | Path, kind: str, occurred_at: datetime, fields: dict[str, object]
) -> dict[str, object]:
    return _service(data_home).preview(kind, occurred_at, fields)


def confirm_training_memory(
    data_home: str | Path, kind: str, occurred_at: datetime,
    fields: dict[str, object], preview_hash: str,
) -> dict[str, object]:
    return _service(data_home).confirm(kind, occurred_at, fields, preview_hash)


def list_training_memory(
    data_home: str | Path, *, days: int = 28, limit: int = 30
) -> dict[str, object]:
    return _service(data_home).list_recent(days=days, limit=limit)
