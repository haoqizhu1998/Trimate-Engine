"""Profile-local, versioned journal for training decisions and responses."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path

from athlete_growth.adapters.storage.sqlite_records import SQLiteRecordStore


class SQLiteGrowthRepository:
    def __init__(self, database_file: Path, records: SQLiteRecordStore) -> None:
        self.database_file = database_file
        self.records = records
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS growth_decisions (
                    decision_id TEXT PRIMARY KEY,
                    athlete_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    preview_hash TEXT NOT NULL,
                    selected_option TEXT NOT NULL,
                    recommended_option TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    supersedes_id TEXT,
                    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_growth_decision_current_day
                    ON growth_decisions(athlete_id, day) WHERE is_current = 1;
                CREATE INDEX IF NOT EXISTS idx_growth_decision_owner
                    ON growth_decisions(athlete_id, created_at);
                CREATE TABLE IF NOT EXISTS growth_feedback (
                    decision_id TEXT PRIMARY KEY REFERENCES growth_decisions(decision_id),
                    athlete_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    completed INTEGER NOT NULL CHECK (completed IN (0, 1)),
                    effect TEXT NOT NULL CHECK (effect IN ('better', 'same', 'worse')),
                    pain_after INTEGER CHECK (pain_after BETWEEN 0 AND 10),
                    note TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS growth_adaptations (
                    adaptation_id TEXT PRIMARY KEY,
                    athlete_id TEXT NOT NULL,
                    preferred_option TEXT NOT NULL CHECK (preferred_option IN ('usual', 'easy')),
                    source_signature TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
                    accepted_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_growth_adaptation_current
                    ON growth_adaptations(athlete_id) WHERE is_current = 1;
                """
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_file)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _decision(row: sqlite3.Row) -> dict[str, object]:
        return {
            "decision_id": row["decision_id"],
            "athlete_id": row["athlete_id"],
            "day": row["day"],
            "preview_hash": row["preview_hash"],
            "selected_option": row["selected_option"],
            "recommended_option": row["recommended_option"],
            "context": json.loads(row["context_json"]),
            "supersedes_id": row["supersedes_id"],
            "is_current": bool(row["is_current"]),
            "created_at": row["created_at"],
        }

    def current_decision(self, athlete_id: str, day: str) -> dict[str, object] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM growth_decisions WHERE athlete_id = ? AND day = ? "
                "AND is_current = 1",
                (athlete_id, day),
            ).fetchone()
        return self._decision(row) if row is not None else None

    def get_decision(self, athlete_id: str, decision_id: str) -> dict[str, object] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM growth_decisions WHERE athlete_id = ? AND decision_id = ?",
                (athlete_id, decision_id),
            ).fetchone()
        return self._decision(row) if row is not None else None

    def list_decisions(self, athlete_id: str, limit: int = 20) -> list[dict[str, object]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM growth_decisions WHERE athlete_id = ? "
                "ORDER BY created_at DESC, decision_id DESC LIMIT ?",
                (athlete_id, limit),
            ).fetchall()
        return [self._decision(row) for row in rows]

    def confirm_decision(
        self,
        *,
        athlete_id: str,
        decision_id: str,
        day: str,
        preview_hash: str,
        selected_option: str,
        recommended_option: str,
        context: dict[str, object],
        expected_current_id: str | None,
        expected_adaptation_id: str | None,
        expected_record_ids: tuple[str, ...],
        now: datetime,
    ) -> dict[str, object]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM growth_decisions WHERE decision_id = ?", (decision_id,)
            ).fetchone()
            if existing is not None:
                if existing["athlete_id"] != athlete_id or existing["preview_hash"] != preview_hash:
                    raise ValueError("decision identity conflict")
                if not existing["is_current"]:
                    raise ValueError("decision was superseded; preview it again")
                return self._decision(existing)
            current_record_ids = self.records.list_current_ids(
                athlete_id, connection=connection
            )
            if current_record_ids != expected_record_ids:
                raise ValueError("source data changed; preview the decision again")
            current = connection.execute(
                "SELECT decision_id FROM growth_decisions WHERE athlete_id = ? "
                "AND day = ? AND is_current = 1",
                (athlete_id, day),
            ).fetchone()
            current_id = current["decision_id"] if current is not None else None
            if current_id != expected_current_id:
                raise ValueError("decision changed; preview it again")
            active = connection.execute(
                "SELECT adaptation_id FROM growth_adaptations "
                "WHERE athlete_id = ? AND is_current = 1",
                (athlete_id,),
            ).fetchone()
            active_id = active["adaptation_id"] if active is not None else None
            if active_id != expected_adaptation_id:
                raise ValueError("adaptation changed; preview the decision again")
            if current_id is not None:
                connection.execute(
                    "UPDATE growth_decisions SET is_current = 0 WHERE decision_id = ?",
                    (current_id,),
                )
            connection.execute(
                """
                INSERT INTO growth_decisions (
                    decision_id, athlete_id, day, preview_hash, selected_option,
                    recommended_option, context_json, supersedes_id, is_current, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    decision_id,
                    athlete_id,
                    day,
                    preview_hash,
                    selected_option,
                    recommended_option,
                    json.dumps(context, ensure_ascii=False, sort_keys=True),
                    current_id,
                    now.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM growth_decisions WHERE decision_id = ?", (decision_id,)
            ).fetchone()
            assert row is not None
            return self._decision(row)

    def feedback(self, athlete_id: str, decision_id: str) -> dict[str, object] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM growth_feedback WHERE athlete_id = ? AND decision_id = ?",
                (athlete_id, decision_id),
            ).fetchone()
        return self._feedback(row) if row is not None else None

    @staticmethod
    def _feedback(row: sqlite3.Row) -> dict[str, object]:
        return {
            "decision_id": row["decision_id"],
            "athlete_id": row["athlete_id"],
            "revision": row["revision"],
            "completed": bool(row["completed"]),
            "effect": row["effect"],
            "pain_after": row["pain_after"],
            "note": row["note"],
            "recorded_at": row["recorded_at"],
        }

    def save_feedback(
        self,
        *,
        athlete_id: str,
        decision_id: str,
        expected_revision: int,
        completed: bool,
        effect: str,
        pain_after: int | None,
        note: str,
        now: datetime,
    ) -> dict[str, object]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            decision = connection.execute(
                "SELECT is_current FROM growth_decisions WHERE athlete_id = ? AND decision_id = ?",
                (athlete_id, decision_id),
            ).fetchone()
            if decision is None:
                raise KeyError("decision not found")
            if not decision["is_current"]:
                raise ValueError("decision was superseded; use the current decision")
            previous = connection.execute(
                "SELECT * FROM growth_feedback WHERE athlete_id = ? AND decision_id = ?",
                (athlete_id, decision_id),
            ).fetchone()
            current_revision = previous["revision"] if previous is not None else 0
            if current_revision != expected_revision:
                if previous is not None and all(
                    (
                        bool(previous["completed"]) == completed,
                        previous["effect"] == effect,
                        previous["pain_after"] == pain_after,
                        previous["note"] == note,
                    )
                ):
                    return self._feedback(previous)
                raise ValueError("feedback revision changed; read it again")
            revision = current_revision + 1
            connection.execute(
                """
                INSERT INTO growth_feedback (
                    decision_id, athlete_id, revision, completed, effect,
                    pain_after, note, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(decision_id) DO UPDATE SET
                    revision = excluded.revision, completed = excluded.completed,
                    effect = excluded.effect, pain_after = excluded.pain_after,
                    note = excluded.note, recorded_at = excluded.recorded_at
                """,
                (
                    decision_id,
                    athlete_id,
                    revision,
                    int(completed),
                    effect,
                    pain_after,
                    note,
                    now.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM growth_feedback WHERE decision_id = ?", (decision_id,)
            ).fetchone()
            assert row is not None
            return self._feedback(row)

    def recent_outcomes(self, athlete_id: str, since: datetime) -> list[dict[str, object]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT d.decision_id, d.day, d.selected_option, d.context_json, f.revision,
                       f.completed, f.effect, f.pain_after, f.recorded_at
                FROM growth_decisions d JOIN growth_feedback f
                  ON d.decision_id = f.decision_id
                WHERE d.athlete_id = ? AND d.is_current = 1
                  AND d.day >= ? AND f.recorded_at >= ?
                ORDER BY f.recorded_at, d.decision_id
                """,
                (athlete_id, since.date().isoformat(), since.isoformat()),
            ).fetchall()
        outcomes: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            context = json.loads(str(item.pop("context_json")))
            item["risk_level"] = context.get("risk_level")
            outcomes.append(item)
        return outcomes

    def source_signature(self, athlete_id: str, now: datetime) -> str:
        with self._connection() as connection:
            return self._source_signature(connection, athlete_id, now)

    def active_adaptation(self, athlete_id: str) -> dict[str, object] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM growth_adaptations WHERE athlete_id = ? AND is_current = 1",
                (athlete_id,),
            ).fetchone()
        return self._adaptation(row) if row is not None else None

    @staticmethod
    def _adaptation(row: sqlite3.Row) -> dict[str, object]:
        return {
            "adaptation_id": row["adaptation_id"],
            "preferred_option": row["preferred_option"],
            "source_signature": row["source_signature"],
            "evidence_decision_ids": json.loads(row["evidence_json"]),
            "accepted_at": row["accepted_at"],
            "revoked_at": row["revoked_at"],
        }

    def accept_adaptation(
        self,
        *,
        athlete_id: str,
        adaptation_id: str,
        source_signature: str,
        evidence_decision_ids: list[str],
        now: datetime,
    ) -> dict[str, object]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM growth_adaptations WHERE adaptation_id = ? AND athlete_id = ?",
                (adaptation_id, athlete_id),
            ).fetchone()
            if existing is not None and existing["is_current"]:
                return self._adaptation(existing)
            if self._source_signature(connection, athlete_id, now) != source_signature:
                raise ValueError("source data changed; review the adaptation again")
            active = connection.execute(
                "SELECT adaptation_id FROM growth_adaptations "
                "WHERE athlete_id = ? AND is_current = 1",
                (athlete_id,),
            ).fetchone()
            if active is not None:
                raise ValueError("adaptation state changed; review it again")
            connection.execute(
                "UPDATE growth_adaptations SET is_current = 0 WHERE athlete_id = ? "
                "AND is_current = 1",
                (athlete_id,),
            )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO growth_adaptations (
                        adaptation_id, athlete_id, preferred_option, source_signature,
                        evidence_json, is_current, accepted_at, revoked_at
                    ) VALUES (?, ?, 'easy', ?, ?, 1, ?, NULL)
                    """,
                    (
                        adaptation_id,
                        athlete_id,
                        source_signature,
                        json.dumps(evidence_decision_ids),
                        now.isoformat(),
                    ),
                )
            else:
                connection.execute(
                    "UPDATE growth_adaptations SET is_current = 1, revoked_at = NULL, "
                    "accepted_at = ? WHERE adaptation_id = ?",
                    (now.isoformat(), adaptation_id),
                )
            row = connection.execute(
                "SELECT * FROM growth_adaptations WHERE adaptation_id = ?",
                (adaptation_id,),
            ).fetchone()
            assert row is not None
            return self._adaptation(row)

    def revoke_adaptation(self, athlete_id: str, now: datetime) -> bool:
        with self._connection() as connection:
            updated = connection.execute(
                "UPDATE growth_adaptations SET is_current = 0, revoked_at = ? "
                "WHERE athlete_id = ? AND is_current = 1",
                (now.isoformat(), athlete_id),
            )
            return updated.rowcount == 1

    @staticmethod
    def _feedback_signature(connection: sqlite3.Connection, athlete_id: str, now: datetime) -> str:
        since = (now - timedelta(days=90)).isoformat()
        rows = connection.execute(
            """
            SELECT d.decision_id, f.revision, f.completed, f.effect, f.pain_after
            FROM growth_decisions d JOIN growth_feedback f
              ON d.decision_id = f.decision_id
            WHERE d.athlete_id = ? AND d.is_current = 1
              AND d.day >= ? AND f.recorded_at >= ?
            ORDER BY d.decision_id
            """,
            (athlete_id, (now - timedelta(days=90)).date().isoformat(), since),
        ).fetchall()
        payload = [tuple(row) for row in rows]
        return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _source_signature(
        self, connection: sqlite3.Connection, athlete_id: str, now: datetime
    ) -> str:
        payload = [
            self._feedback_signature(connection, athlete_id, now),
            self.records.list_current_ids(athlete_id, connection=connection),
        ]
        return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
