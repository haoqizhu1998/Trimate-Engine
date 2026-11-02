"""Profile-local transactional draft storage for conversational planning."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from athlete_growth.adapters.storage.sqlite_records import SQLiteRecordStore
from athlete_growth.domain.records import CanonicalRecord
from athlete_growth.ports.planning import PlanningDraft


class SQLitePlanningDraftRepository:
    def __init__(self, database_file: Path, records: SQLiteRecordStore) -> None:
        self.database_file = database_file
        self.records = records
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS planning_drafts (
                    draft_id TEXT PRIMARY KEY,
                    athlete_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('goal', 'plan')),
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    status TEXT NOT NULL CHECK (status IN ('draft', 'confirmed')),
                    data_json TEXT NOT NULL,
                    confirmed_record_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_planning_drafts_owner "
                "ON planning_drafts(athlete_id, kind, status)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_file)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _decode(row: sqlite3.Row) -> PlanningDraft:
        return PlanningDraft(
            row["draft_id"],
            row["athlete_id"],
            row["kind"],
            row["revision"],
            row["status"],
            json.loads(row["data_json"]),
            row["confirmed_record_id"],
        )

    def create(self, draft: PlanningDraft) -> PlanningDraft:
        if draft.revision != 1 or draft.status != "draft" or draft.confirmed_record_id:
            raise ValueError("new planning draft must start at revision 1")
        now = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO planning_drafts (
                    draft_id, athlete_id, kind, revision, status, data_json,
                    confirmed_record_id, created_at, updated_at
                ) VALUES (?, ?, ?, 1, 'draft', ?, NULL, ?, ?)
                """,
                (
                    draft.draft_id,
                    draft.athlete_id,
                    draft.kind,
                    json.dumps(draft.data, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
        return draft

    def get(self, athlete_id: str, kind: str, draft_id: str) -> PlanningDraft | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM planning_drafts WHERE draft_id = ? AND athlete_id = ? AND kind = ?",
                (draft_id, athlete_id, kind),
            ).fetchone()
        return self._decode(row) if row is not None else None

    def list_open(self, athlete_id: str) -> list[PlanningDraft]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM planning_drafts WHERE athlete_id = ? AND status = 'draft' "
                "ORDER BY updated_at DESC, draft_id",
                (athlete_id,),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def revise(self, draft: PlanningDraft, *, expected_revision: int) -> PlanningDraft:
        if draft.status != "draft" or draft.revision != expected_revision + 1:
            raise ValueError("invalid draft revision")
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE planning_drafts SET revision = ?, data_json = ?, updated_at = ?
                WHERE draft_id = ? AND athlete_id = ? AND kind = ?
                  AND revision = ? AND status = 'draft'
                """,
                (
                    draft.revision,
                    json.dumps(draft.data, ensure_ascii=False, sort_keys=True),
                    datetime.now(UTC).isoformat(),
                    draft.draft_id,
                    draft.athlete_id,
                    draft.kind,
                    expected_revision,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("draft changed or is no longer editable")
        return draft

    def confirm(
        self,
        athlete_id: str,
        kind: str,
        draft_id: str,
        expected_revision: int,
        record: CanonicalRecord,
        *,
        expected_current_ids: tuple[str, ...] | None = None,
    ) -> CanonicalRecord:
        if record.athlete_id != athlete_id or record.kind != (
            "goal" if kind == "goal" else "training-plan"
        ):
            raise ValueError("confirmed record does not match draft owner and kind")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM planning_drafts WHERE draft_id = ? AND athlete_id = ? AND kind = ?",
                (draft_id, athlete_id, kind),
            ).fetchone()
            if row is None:
                raise KeyError("draft not found")
            draft = self._decode(row)
            if draft.revision != expected_revision:
                raise ValueError("draft revision changed; review the latest draft")
            if draft.status == "confirmed":
                if draft.confirmed_record_id is None:
                    raise RuntimeError("confirmed draft has no record")
                existing = self.records.get(draft.confirmed_record_id, connection=connection)
                if existing is None:
                    raise RuntimeError("confirmed record is missing")
                connection.commit()
                return existing
            if draft.status != "draft":
                raise ValueError("draft cannot be confirmed")
            if expected_current_ids is not None:
                current_ids = self.records.list_current_ids(athlete_id, connection=connection)
                if current_ids != expected_current_ids:
                    raise ValueError("source data changed; preview the plan again")
            if not self.records.save(record, connection=connection):
                raise ValueError("confirmed record already exists")
            connection.execute(
                """
                UPDATE planning_drafts SET status = 'confirmed', confirmed_record_id = ?,
                    updated_at = ? WHERE draft_id = ?
                """,
                (record.record_id, datetime.now(UTC).isoformat(), draft_id),
            )
            connection.commit()
            return record
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def confirm_goal_change(
        self,
        athlete_id: str,
        draft_id: str,
        expected_revision: int,
        target_record_id: str,
        replacement: CanonicalRecord | None,
        *,
        expected_current_ids: tuple[str, ...],
    ) -> CanonicalRecord:
        """Retire an old goal and optionally insert its replacement atomically."""
        if replacement is not None and (
            replacement.athlete_id != athlete_id or replacement.kind != "goal"
        ):
            raise ValueError("replacement goal does not match profile")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM planning_drafts WHERE draft_id = ? AND athlete_id = ? "
                "AND kind = 'goal'",
                (draft_id, athlete_id),
            ).fetchone()
            if row is None:
                raise KeyError("draft not found")
            draft = self._decode(row)
            if draft.revision != expected_revision:
                raise ValueError("draft revision changed; review the latest draft")
            if draft.status == "confirmed":
                saved = self.records.get(str(draft.confirmed_record_id), connection=connection)
                if saved is None:
                    raise RuntimeError("confirmed record is missing")
                connection.commit()
                return saved
            if draft.status != "draft" or draft.data.get("action") not in {"replace", "delete"}:
                raise ValueError("draft cannot be confirmed")
            if draft.data.get("target_record_id") != target_record_id:
                raise ValueError("goal changed; preview the goal change again")
            if (
                self.records.list_current_ids(athlete_id, connection=connection)
                != expected_current_ids
            ):
                raise ValueError("source data changed; preview the goal change again")
            target = connection.execute(
                "SELECT record_id FROM canonical_records WHERE record_id = ? AND athlete_id = ? "
                "AND kind = 'goal' AND is_current = 1",
                (target_record_id, athlete_id),
            ).fetchone()
            if target is None:
                raise ValueError("goal changed; preview the goal change again")
            self.records.retire(target_record_id, connection=connection)
            if replacement is not None and not self.records.save(
                replacement, connection=connection
            ):
                raise ValueError("replacement goal already exists")
            saved = replacement or self.records.get(target_record_id, connection=connection)
            assert saved is not None
            connection.execute(
                "UPDATE planning_drafts SET status = 'confirmed', "
                "confirmed_record_id = ?, updated_at = ? "
                "WHERE draft_id = ?",
                (saved.record_id, datetime.now(UTC).isoformat(), draft_id),
            )
            connection.commit()
            return saved
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
