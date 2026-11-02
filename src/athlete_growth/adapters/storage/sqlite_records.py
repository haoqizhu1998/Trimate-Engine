"""SQLite persistence for canonical records."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from athlete_growth.domain.records import CanonicalRecord, EvidenceRef


class SQLiteRecordStore:
    """Profile-local store for versioned canonical records."""

    def __init__(self, database_file: Path) -> None:
        self.database_file = database_file
        database_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(database_file.parent, 0o700)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_file)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def _borrow_connection(
        self,
        connection: sqlite3.Connection | None,
    ) -> Iterator[sqlite3.Connection]:
        """Use a caller-owned transaction or open and commit a local one."""

        if connection is not None:
            yield connection
            return
        with self._connection() as owned:
            yield owned

    def _migrate(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS canonical_records (
                    record_id TEXT PRIMARY KEY,
                    athlete_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    quality_flags_json TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    supersedes_id TEXT,
                    created_at TEXT NOT NULL,
                    is_current INTEGER NOT NULL DEFAULT 1
                        CHECK (is_current IN (0, 1))
                );
                CREATE INDEX IF NOT EXISTS idx_canonical_current
                    ON canonical_records(athlete_id, kind, occurred_at, is_current);

                CREATE TABLE IF NOT EXISTS sync_cursors (
                    athlete_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    cursor TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (athlete_id, source)
                );
                """
            )
        os.chmod(self.database_file, 0o600)

    def save(
        self,
        record: CanonicalRecord,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        with self._borrow_connection(connection) as active_connection:
            cursor = active_connection.execute(
                """
                INSERT OR IGNORE INTO canonical_records (
                    record_id, athlete_id, kind, occurred_at, data_json,
                    evidence_json, quality_flags_json, schema_version,
                    supersedes_id, created_at, is_current
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                self._serialize(record),
            )
            return cursor.rowcount == 1

    def replace_singleton(
        self,
        athlete_id: str,
        kind: str,
        expected_record_id: str | None,
        replacement: CanonicalRecord,
    ) -> CanonicalRecord:
        """Compare and replace one profile-local projection without losing history."""
        if replacement.athlete_id != athlete_id or replacement.kind != kind:
            raise ValueError("replacement does not match profile and kind")
        if replacement.supersedes_id != expected_record_id:
            raise ValueError("replacement does not identify the prior record")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT record_id FROM canonical_records WHERE athlete_id = ? "
                "AND kind = ? AND is_current = 1",
                (athlete_id, kind),
            ).fetchall()
            current_ids = [str(row["record_id"]) for row in rows]
            if current_ids != ([expected_record_id] if expected_record_id else []):
                raise ValueError("training preferences changed; preview them again")
            if expected_record_id is not None:
                self.retire(expected_record_id, connection=connection)
            if not self.save(replacement, connection=connection):
                raise ValueError("training preference version already exists")
            connection.commit()
            return replacement
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def correct(
        self,
        original_id: str,
        replacement: CanonicalRecord,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if replacement.supersedes_id != original_id:
            raise ValueError("replacement must identify the record it supersedes")
        with self._borrow_connection(connection) as active_connection:
            updated = active_connection.execute(
                "UPDATE canonical_records SET is_current = 0 WHERE record_id = ?",
                (original_id,),
            )
            if updated.rowcount != 1:
                raise KeyError(original_id)
            active_connection.execute(
                """
                INSERT INTO canonical_records (
                    record_id, athlete_id, kind, occurred_at, data_json,
                    evidence_json, quality_flags_json, schema_version,
                    supersedes_id, created_at, is_current
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                self._serialize(replacement),
            )

    def retire(
        self,
        record_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """Keep a record as history while removing it from current projections."""
        with self._borrow_connection(connection) as active_connection:
            updated = active_connection.execute(
                "UPDATE canonical_records SET is_current = 0 "
                "WHERE record_id = ? AND is_current = 1",
                (record_id,),
            )
            if updated.rowcount != 1:
                raise KeyError(record_id)

    def get(
        self,
        record_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> CanonicalRecord | None:
        with self._borrow_connection(connection) as active_connection:
            row = active_connection.execute(
                "SELECT * FROM canonical_records WHERE record_id = ?", (record_id,)
            ).fetchone()
        return self._deserialize(row) if row is not None else None

    def list_current(
        self,
        athlete_id: str,
        *,
        kind: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> list[CanonicalRecord]:
        clauses = ["athlete_id = ?", "is_current = 1"]
        parameters: list[object] = [athlete_id]
        if kind is not None:
            clauses.append("kind = ?")
            parameters.append(kind)
        if start is not None:
            clauses.append("occurred_at >= ?")
            parameters.append(start.isoformat())
        if end is not None:
            clauses.append("occurred_at <= ?")
            parameters.append(end.isoformat())
        query = (
            "SELECT * FROM canonical_records WHERE "
            + " AND ".join(clauses)
            + " ORDER BY occurred_at, record_id"
        )
        with self._borrow_connection(connection) as active_connection:
            rows = active_connection.execute(query, parameters).fetchall()
        return [self._deserialize(row) for row in rows]

    def list_current_ids(
        self,
        athlete_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[str, ...]:
        """Return the current projection identity using the caller's transaction."""

        with self._borrow_connection(connection) as active_connection:
            rows = active_connection.execute(
                """
                SELECT record_id FROM canonical_records
                WHERE athlete_id = ? AND is_current = 1
                ORDER BY record_id
                """,
                (athlete_id,),
            ).fetchall()
        return tuple(row["record_id"] for row in rows)

    def list_by_ids(
        self,
        athlete_id: str,
        record_ids: tuple[str, ...],
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> list[CanonicalRecord]:
        """Read exact historical versions from a durable projection watermark."""

        selected = set(record_ids)
        if not selected:
            return []
        clauses = ["athlete_id = ?"]
        parameters: list[object] = [athlete_id]
        if start is not None:
            clauses.append("occurred_at >= ?")
            parameters.append(start.isoformat())
        if end is not None:
            clauses.append("occurred_at <= ?")
            parameters.append(end.isoformat())
        query = (
            "SELECT * FROM canonical_records WHERE "
            + " AND ".join(clauses)
            + " ORDER BY occurred_at, record_id"
        )
        with self._borrow_connection(connection) as active_connection:
            rows = active_connection.execute(query, parameters).fetchall()
        return [self._deserialize(row) for row in rows if row["record_id"] in selected]

    def get_cursor(
        self,
        athlete_id: str,
        source: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> str | None:
        with self._borrow_connection(connection) as active_connection:
            row = active_connection.execute(
                "SELECT cursor FROM sync_cursors WHERE athlete_id = ? AND source = ?",
                (athlete_id, source),
            ).fetchone()
        return row["cursor"] if row else None

    def set_cursor(
        self,
        athlete_id: str,
        source: str,
        cursor: str,
        updated_at: datetime,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        with self._borrow_connection(connection) as active_connection:
            active_connection.execute(
                """
                INSERT INTO sync_cursors(athlete_id, source, cursor, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(athlete_id, source) DO UPDATE SET
                    cursor = excluded.cursor,
                    updated_at = excluded.updated_at
                """,
                (athlete_id, source, cursor, updated_at.isoformat()),
            )

    @staticmethod
    def _serialize(record: CanonicalRecord) -> tuple[object, ...]:
        evidence = [
            {
                "source": item.source,
                "source_id": item.source_id,
                "captured_at": item.captured_at.isoformat(),
                "raw_hash": item.raw_hash,
            }
            for item in record.evidence
        ]
        return (
            record.record_id,
            record.athlete_id,
            record.kind,
            record.occurred_at.isoformat(),
            json.dumps(record.data, ensure_ascii=False, sort_keys=True),
            json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            json.dumps(record.quality_flags, ensure_ascii=False),
            record.schema_version,
            record.supersedes_id,
            record.created_at.isoformat(),
        )

    @staticmethod
    def _deserialize(row: sqlite3.Row) -> CanonicalRecord:
        evidence = tuple(
            EvidenceRef(
                source=item["source"],
                source_id=item["source_id"],
                captured_at=datetime.fromisoformat(item["captured_at"]),
                raw_hash=item.get("raw_hash"),
            )
            for item in json.loads(row["evidence_json"])
        )
        return CanonicalRecord(
            record_id=row["record_id"],
            athlete_id=row["athlete_id"],
            kind=row["kind"],
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            data=json.loads(row["data_json"]),
            evidence=evidence,
            quality_flags=tuple(json.loads(row["quality_flags_json"])),
            schema_version=row["schema_version"],
            supersedes_id=row["supersedes_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )
