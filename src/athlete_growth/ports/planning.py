"""Host-independent persistence contract for goal and plan drafts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from athlete_growth.domain.records import CanonicalRecord, JsonValue


@dataclass(frozen=True, slots=True)
class PlanningDraft:
    draft_id: str
    athlete_id: str
    kind: str
    revision: int
    status: str
    data: dict[str, JsonValue]
    confirmed_record_id: str | None = None


class PlanningDraftRepository(Protocol):
    def create(self, draft: PlanningDraft) -> PlanningDraft: ...

    def get(self, athlete_id: str, kind: str, draft_id: str) -> PlanningDraft | None: ...

    def list_open(self, athlete_id: str) -> list[PlanningDraft]: ...

    def revise(
        self, draft: PlanningDraft, *, expected_revision: int
    ) -> PlanningDraft: ...

    def confirm(
        self,
        athlete_id: str,
        kind: str,
        draft_id: str,
        expected_revision: int,
        record: CanonicalRecord,
        *,
        expected_current_ids: tuple[str, ...] | None = None,
    ) -> CanonicalRecord: ...

    def confirm_goal_change(
        self,
        athlete_id: str,
        draft_id: str,
        expected_revision: int,
        target_record_id: str,
        replacement: CanonicalRecord | None,
        *,
        expected_current_ids: tuple[str, ...],
    ) -> CanonicalRecord: ...
