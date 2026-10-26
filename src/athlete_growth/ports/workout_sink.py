"""Confirmation-gated workout writeback port."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from athlete_growth.domain.records import JsonValue


class ConfirmationRequired(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WorkoutPreview:
    preview_id: str
    payload: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class WorkoutWriteReceipt:
    preview_id: str
    remote_id: str
    confirmed: bool


class ConfirmationGatedWorkoutSink:
    """Require a preview and explicit confirmation before any remote write."""

    def __init__(self, writer: Callable[[dict[str, JsonValue]], str]) -> None:
        self._writer = writer

    def preview(self, payload: dict[str, JsonValue]) -> WorkoutPreview:
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return WorkoutPreview(hashlib.sha256(serialized.encode()).hexdigest(), payload)

    def write(self, preview: WorkoutPreview, *, confirmed: bool) -> WorkoutWriteReceipt:
        if not confirmed:
            raise ConfirmationRequired("remote workout write requires confirmation")
        remote_id = self._writer(dict(preview.payload))
        return WorkoutWriteReceipt(preview.preview_id, remote_id, True)
