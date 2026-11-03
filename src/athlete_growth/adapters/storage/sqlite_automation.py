"""SQLite persistence for restart-safe automation state."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from athlete_growth.application.automation.models import (
    LEGACY_PROJECTION_UNAVAILABLE,
    AnalysisSnapshot,
    DataChangeSet,
    NotificationMode,
    SyncRun,
    SyncSubscription,
    UserNotification,
)
from athlete_growth.domain.records import JsonValue

if TYPE_CHECKING:
    from athlete_growth.application.ingest.service import IngestionResult


class SQLiteAutomationRepository:
    """Persist scheduling, sync, analysis, and notification state atomically."""

    _SCHEMA_VERSION = 4

    def __init__(self, database_file: Path) -> None:
        self.database_file = database_file
        database_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(database_file.parent, 0o700)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_file)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _migrate(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            tables = self._table_names(connection)
            version: int | None = None
            if "automation_schema_version" in tables:
                row = connection.execute(
                    "SELECT version FROM automation_schema_version WHERE singleton = 1"
                ).fetchone()
                version = row["version"] if row is not None else None

            core_tables = {
                "sync_subscriptions",
                "automation_sync_runs",
                "data_change_sets",
                "analysis_snapshots",
                "notification_inbox",
            }
            should_set_version = False
            if not tables.intersection(core_tables):
                self._create_schema(connection)
                should_set_version = True
            elif version == self._SCHEMA_VERSION:
                required_run_columns = {
                    "request_key",
                    "lease_token",
                    "lease_owner",
                    "lease_expires_at",
                    "updated_at",
                }
                if not required_run_columns <= self._table_columns(
                    connection, "automation_sync_runs"
                ):
                    raise ValueError(
                        f"automation schema version {self._SCHEMA_VERSION} is incomplete"
                    )
                if "projection_record_ids_json" not in self._table_columns(
                    connection, "data_change_sets"
                ):
                    raise ValueError(
                        f"automation schema version {self._SCHEMA_VERSION} is incomplete"
                    )
                required_notification_columns = {
                    "next_attempt_at",
                    "delivery_lease_token",
                    "delivery_lease_owner",
                    "delivery_lease_expires_at",
                }
                if not required_notification_columns <= self._table_columns(
                    connection, "notification_inbox"
                ):
                    raise ValueError(
                        f"automation schema version {self._SCHEMA_VERSION} is incomplete"
                    )
                self._create_schema(connection)
            elif version is None or version < self._SCHEMA_VERSION:
                self._rebuild_schema(connection, tables, source_version=version or 1)
                should_set_version = True
            else:
                raise ValueError(f"unsupported automation schema version: {version}")

            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_errors:
                raise ValueError("automation schema migration failed foreign key validation")
            if should_set_version:
                self._set_schema_version(connection)
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
            if self.database_file.exists():
                os.chmod(self.database_file, 0o600)

    @staticmethod
    def _table_names(connection: sqlite3.Connection) -> set[str]:
        return {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    @classmethod
    def _create_schema(cls, connection: sqlite3.Connection) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS automation_schema_version (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                version INTEGER NOT NULL CHECK (version > 0)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS sync_subscriptions (
                athlete_id TEXT NOT NULL,
                source TEXT NOT NULL,
                interval_minutes INTEGER NOT NULL CHECK (interval_minutes > 0),
                lookback_days INTEGER NOT NULL CHECK (lookback_days > 0),
                notification_mode TEXT NOT NULL
                    CHECK (notification_mode IN ('actionable-digest', 'all', 'off')),
                digest_hour_local INTEGER NOT NULL
                    CHECK (digest_hour_local BETWEEN 0 AND 23),
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                next_run_at TEXT NOT NULL,
                PRIMARY KEY (athlete_id, source)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS automation_sync_runs (
                run_id TEXT PRIMARY KEY,
                athlete_id TEXT NOT NULL,
                source TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
                started_at TEXT NOT NULL,
                completed_at TEXT,
                error TEXT,
                request_key TEXT,
                lease_token TEXT,
                lease_owner TEXT,
                lease_expires_at TEXT,
                updated_at TEXT NOT NULL,
                CHECK (
                    (status = 'running' AND completed_at IS NULL AND error IS NULL
                        AND (
                            (lease_token IS NULL AND lease_owner IS NULL
                                AND lease_expires_at IS NULL)
                            OR (lease_token IS NOT NULL AND length(trim(lease_token)) > 0
                                AND lease_expires_at IS NOT NULL)
                        ))
                    OR (status = 'completed' AND completed_at IS NOT NULL AND error IS NULL
                        AND lease_token IS NULL AND lease_owner IS NULL
                        AND lease_expires_at IS NULL)
                    OR (status = 'failed' AND completed_at IS NOT NULL
                        AND error IS NOT NULL AND length(trim(error)) > 0
                        AND lease_token IS NULL
                        AND lease_owner IS NULL AND lease_expires_at IS NULL)
                ),
                CHECK (completed_at IS NULL OR completed_at >= started_at),
                CHECK (lease_expires_at IS NULL OR lease_expires_at >= started_at),
                CHECK (updated_at >= started_at)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS data_change_sets (
                change_set_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL UNIQUE,
                athlete_id TEXT NOT NULL,
                source TEXT NOT NULL,
                record_ids_json TEXT NOT NULL,
                affected_dates_json TEXT NOT NULL,
                projection_record_ids_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES automation_sync_runs(run_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS analysis_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                change_set_id TEXT NOT NULL,
                athlete_id TEXT NOT NULL,
                period TEXT NOT NULL,
                analysis_kind TEXT NOT NULL,
                algorithm_version TEXT NOT NULL,
                report_json TEXT NOT NULL,
                risk_level TEXT NOT NULL
                    CHECK (risk_level IN ('unknown', 'low', 'moderate', 'high')),
                created_at TEXT NOT NULL,
                UNIQUE (change_set_id, period, analysis_kind, algorithm_version),
                FOREIGN KEY (change_set_id) REFERENCES data_change_sets(change_set_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS notification_inbox (
                notification_id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL,
                athlete_id TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                channel TEXT NOT NULL,
                priority TEXT NOT NULL CHECK (priority IN ('actionable', 'digest')),
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                deliver_after TEXT,
                created_at TEXT NOT NULL,
                read_at TEXT,
                delivered_at TEXT,
                delivery_attempts INTEGER NOT NULL DEFAULT 0 CHECK (delivery_attempts >= 0),
                last_error TEXT,
                next_attempt_at TEXT NOT NULL,
                delivery_lease_token TEXT,
                delivery_lease_owner TEXT,
                delivery_lease_expires_at TEXT,
                CHECK (
                    (delivery_lease_token IS NULL AND delivery_lease_owner IS NULL
                        AND delivery_lease_expires_at IS NULL)
                    OR (delivery_lease_token IS NOT NULL
                        AND length(trim(delivery_lease_token)) > 0
                        AND delivery_lease_owner IS NOT NULL
                        AND length(trim(delivery_lease_owner)) > 0
                        AND delivery_lease_expires_at IS NOT NULL)
                ),
                UNIQUE (snapshot_id, policy_version, channel),
                FOREIGN KEY (snapshot_id) REFERENCES analysis_snapshots(snapshot_id)
            )
            """,
        )
        for statement in statements:
            connection.execute(statement)
        index_statements = (
            "CREATE INDEX IF NOT EXISTS idx_sync_subscriptions_schema_v4 "
            "ON sync_subscriptions(enabled, next_run_at, athlete_id, source)",
            "CREATE INDEX IF NOT EXISTS idx_automation_sync_runs_latest_schema_v4 "
            "ON automation_sync_runs(athlete_id, source, started_at DESC, run_id DESC)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_automation_sync_runs_request_schema_v4 "
            "ON automation_sync_runs(request_key) WHERE request_key IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_automation_sync_runs_recoverable_schema_v4 "
            "ON automation_sync_runs(status, lease_expires_at, started_at, run_id)",
            "CREATE INDEX IF NOT EXISTS idx_analysis_snapshots_athlete_schema_v4 "
            "ON analysis_snapshots("
            "athlete_id, created_at, analysis_kind, algorithm_version, snapshot_id)",
            "CREATE INDEX IF NOT EXISTS idx_notification_inbox_unread_schema_v4 "
            "ON notification_inbox(athlete_id, read_at, created_at, notification_id)",
            "CREATE INDEX IF NOT EXISTS idx_notification_inbox_pending_schema_v4 "
            "ON notification_inbox(delivered_at, next_attempt_at, delivery_lease_expires_at, "
            "created_at, notification_id)",
        )
        for statement in index_statements:
            connection.execute(statement)

    @classmethod
    def _set_schema_version(cls, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            INSERT INTO automation_schema_version (singleton, version) VALUES (1, ?)
            ON CONFLICT(singleton) DO UPDATE SET version = excluded.version
            """,
            (cls._SCHEMA_VERSION,),
        )

    @classmethod
    def _rebuild_schema(
        cls,
        connection: sqlite3.Connection,
        tables: set[str],
        *,
        source_version: int,
    ) -> None:
        ordered_tables = (
            "sync_subscriptions",
            "automation_sync_runs",
            "data_change_sets",
            "analysis_snapshots",
            "notification_inbox",
        )
        missing = set(ordered_tables) - tables
        if missing:
            raise ValueError(f"incomplete legacy automation schema: {sorted(missing)}")

        source_rows = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in ordered_tables
        }
        source_columns = {table: cls._table_columns(connection, table) for table in ordered_tables}
        normalized_runs = [
            cls._normalize_legacy_run(row, source_columns["automation_sync_runs"])
            for row in source_rows["automation_sync_runs"]
        ]
        runs_by_id = {run.run_id: run for run in normalized_runs}

        backup_tables = {
            table: f"{table}_automation_v{source_version}_backup" for table in ordered_tables
        }
        for target in backup_tables.values():
            if target in tables:
                raise ValueError("interrupted automation migration requires manual recovery")
        for table in reversed(ordered_tables):
            connection.execute(f"ALTER TABLE {table} RENAME TO {backup_tables[table]}")

        cls._create_schema(connection)
        for row in source_rows["sync_subscriptions"]:
            connection.execute(
                "INSERT INTO sync_subscriptions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(row),
            )
        for run in normalized_runs:
            cls._insert_sync_run(connection, run)
        change_set_columns = source_columns["data_change_sets"]
        superseded_change_set_ids: set[str] = set()
        for row in source_rows["data_change_sets"]:
            run = runs_by_id[row["run_id"]]
            projection_record_ids = (
                tuple(json.loads(row["projection_record_ids_json"]))
                if "projection_record_ids_json" in change_set_columns
                else cls._reconstruct_legacy_projection(
                    connection,
                    tables,
                    athlete_id=row["athlete_id"],
                )
            )
            change_set_id = row["change_set_id"]
            created_at = row["created_at"]
            if run.status == "running":
                projection_record_ids = cls._reconstruct_legacy_projection(
                    connection,
                    tables,
                    athlete_id=row["athlete_id"],
                )
                change_set_id = cls._identifier(
                    "migration-recovery-change-set",
                    row["run_id"],
                    json.dumps(projection_record_ids, separators=(",", ":")),
                )
                created_at = cls._migration_recovery_created_at(
                    connection,
                    tables,
                    athlete_id=row["athlete_id"],
                    fallback=row["created_at"],
                )
                superseded_change_set_ids.add(row["change_set_id"])
            connection.execute(
                """
                INSERT INTO data_change_sets (
                    change_set_id, run_id, athlete_id, source, record_ids_json,
                    affected_dates_json, projection_record_ids_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    change_set_id,
                    row["run_id"],
                    row["athlete_id"],
                    row["source"],
                    row["record_ids_json"],
                    row["affected_dates_json"],
                    json.dumps(projection_record_ids, ensure_ascii=False),
                    created_at,
                ),
            )
        analysis_columns = source_columns["analysis_snapshots"]
        skipped_snapshot_ids: set[str] = set()
        for row in source_rows["analysis_snapshots"]:
            if row["change_set_id"] in superseded_change_set_ids:
                skipped_snapshot_ids.add(row["snapshot_id"])
                continue
            connection.execute(
                """
                INSERT INTO analysis_snapshots (
                    snapshot_id, change_set_id, athlete_id, period, analysis_kind,
                    algorithm_version, report_json, risk_level, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["snapshot_id"],
                    row["change_set_id"],
                    row["athlete_id"],
                    row["period"],
                    row["analysis_kind"] if "analysis_kind" in analysis_columns else "wellness",
                    row["algorithm_version"]
                    if "algorithm_version" in analysis_columns
                    else "wellness-report/1.0",
                    row["report_json"],
                    row["risk_level"],
                    row["created_at"],
                ),
            )

        notification_columns = source_columns["notification_inbox"]
        for row in source_rows["notification_inbox"]:
            if row["snapshot_id"] in skipped_snapshot_ids:
                continue
            connection.execute(
                """
                INSERT INTO notification_inbox (
                    notification_id, snapshot_id, athlete_id, policy_version, channel,
                    priority, title, body, deliver_after, created_at, read_at,
                    delivered_at, delivery_attempts, last_error, next_attempt_at,
                    delivery_lease_token, delivery_lease_owner, delivery_lease_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["notification_id"],
                    row["snapshot_id"],
                    row["athlete_id"],
                    row["policy_version"]
                    if "policy_version" in notification_columns
                    else "notification-policy/1.0",
                    row["channel"] if "channel" in notification_columns else "inbox",
                    row["priority"],
                    row["title"],
                    row["body"],
                    row["deliver_after"],
                    row["created_at"],
                    row["read_at"],
                    row["delivered_at"],
                    row["delivery_attempts"],
                    row["last_error"],
                    row["next_attempt_at"]
                    if "next_attempt_at" in notification_columns
                    else (row["deliver_after"] or row["created_at"]),
                    row["delivery_lease_token"]
                    if "delivery_lease_token" in notification_columns
                    else None,
                    row["delivery_lease_owner"]
                    if "delivery_lease_owner" in notification_columns
                    else None,
                    row["delivery_lease_expires_at"]
                    if "delivery_lease_expires_at" in notification_columns
                    else None,
                ),
            )

        for table, backup in backup_tables.items():
            trigger_base = f"protect_{table}_automation_v{source_version}_backup"
            for operation in ("INSERT", "UPDATE", "DELETE"):
                connection.execute(
                    f"""
                    CREATE TRIGGER {trigger_base}_{operation.lower()}
                    BEFORE {operation} ON {backup}
                    BEGIN
                        SELECT RAISE(ABORT, 'read-only backup');
                    END
                    """
                )

    @classmethod
    def _reconstruct_legacy_projection(
        cls,
        connection: sqlite3.Connection,
        tables: set[str],
        *,
        athlete_id: str,
    ) -> tuple[str, ...]:
        """Rebuild a legacy watermark without mistaking changed IDs for a projection."""

        if "canonical_records" not in tables:
            return (LEGACY_PROJECTION_UNAVAILABLE,)
        required_columns = {"record_id", "athlete_id", "is_current"}
        if not required_columns <= cls._table_columns(connection, "canonical_records"):
            raise ValueError("canonical record schema cannot reconstruct legacy projection")
        rows = connection.execute(
            """
            SELECT record_id FROM canonical_records
            WHERE athlete_id = ? AND is_current = 1
            ORDER BY record_id
            """,
            (athlete_id,),
        ).fetchall()
        return tuple(row["record_id"] for row in rows)

    @classmethod
    def _migration_recovery_created_at(
        cls,
        connection: sqlite3.Connection,
        tables: set[str],
        *,
        athlete_id: str,
        fallback: str,
    ) -> str:
        """Use a deterministic watermark time for a reconstructed current projection."""

        fallback_at = cls._datetime(datetime.fromisoformat(fallback), "created_at")
        if "canonical_records" not in tables:
            return fallback_at.isoformat()
        columns = cls._table_columns(connection, "canonical_records")
        if not {"athlete_id", "is_current", "created_at"} <= columns:
            return fallback_at.isoformat()
        rows = connection.execute(
            """
            SELECT created_at FROM canonical_records
            WHERE athlete_id = ? AND is_current = 1
            """,
            (athlete_id,),
        ).fetchall()
        candidates = [fallback_at]
        candidates.extend(
            cls._datetime(datetime.fromisoformat(row["created_at"]), "created_at") for row in rows
        )
        return max(candidates).isoformat()

    @classmethod
    def _normalize_legacy_run(
        cls,
        row: sqlite3.Row,
        columns: set[str],
    ) -> SyncRun:
        try:
            started_at = cls._datetime(datetime.fromisoformat(row["started_at"]), "started_at")
            completed_at = (
                cls._datetime(datetime.fromisoformat(row["completed_at"]), "completed_at")
                if row["completed_at"] is not None
                else None
            )
            status = row["status"]
            lease_expires_at = None
            lease_owner = None
            lease_token = None
            if status == "running":
                if "lease_expires_at" in columns and row["lease_expires_at"] is not None:
                    lease_expires_at = cls._datetime(
                        datetime.fromisoformat(row["lease_expires_at"]),
                        "lease_expires_at",
                    )
                else:
                    lease_expires_at = started_at
                if "lease_owner" in columns:
                    lease_owner = row["lease_owner"]
                if "lease_token" in columns and row["lease_token"] is not None:
                    lease_token = row["lease_token"]
                else:
                    lease_token = cls._identifier(
                        "migrated-lease-token",
                        row["run_id"],
                        started_at.isoformat(),
                    )
            updated_at = completed_at or started_at
            if "updated_at" in columns and row["updated_at"] is not None:
                updated_at = cls._datetime(
                    datetime.fromisoformat(row["updated_at"]),
                    "updated_at",
                )
            return SyncRun(
                run_id=row["run_id"],
                athlete_id=row["athlete_id"],
                source=row["source"],
                reason=row["reason"],
                status=status,
                started_at=started_at,
                completed_at=completed_at,
                error=row["error"],
                request_key=row["request_key"] if "request_key" in columns else None,
                lease_token=lease_token,
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
                updated_at=updated_at,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid legacy sync run {row['run_id']}: {exc}") from exc

    @staticmethod
    def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}

    def save_subscription(
        self,
        subscription: SyncSubscription,
        *,
        next_run_at: datetime,
    ) -> None:
        next_run = self._datetime_text(next_run_at, "next_run_at")
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO sync_subscriptions (
                    athlete_id, source, interval_minutes, lookback_days,
                    notification_mode, digest_hour_local, enabled, next_run_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(athlete_id, source) DO UPDATE SET
                    interval_minutes = excluded.interval_minutes,
                    lookback_days = excluded.lookback_days,
                    notification_mode = excluded.notification_mode,
                    digest_hour_local = excluded.digest_hour_local,
                    enabled = excluded.enabled,
                    next_run_at = excluded.next_run_at
                """,
                (
                    subscription.athlete_id,
                    subscription.source,
                    subscription.interval_minutes,
                    subscription.lookback_days,
                    subscription.notification_mode.value,
                    subscription.digest_hour_local,
                    int(subscription.enabled),
                    next_run,
                ),
            )

    def schedule_next(
        self,
        subscription: SyncSubscription,
        *,
        next_run_at: datetime,
    ) -> None:
        next_run = self._datetime_text(next_run_at, "next_run_at")
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE sync_subscriptions
                SET next_run_at = ?
                WHERE athlete_id = ? AND source = ?
                """,
                (next_run, subscription.athlete_id, subscription.source),
            )
            if cursor.rowcount != 1:
                raise KeyError((subscription.athlete_id, subscription.source))

    def get_next_run_at(self, athlete_id: str, source: str) -> datetime | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT next_run_at FROM sync_subscriptions
                WHERE athlete_id = ? AND source = ?
                """,
                (athlete_id, source),
            ).fetchone()
        return datetime.fromisoformat(row["next_run_at"]) if row is not None else None

    def get_subscription(self, athlete_id: str, source: str) -> SyncSubscription | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM sync_subscriptions
                WHERE athlete_id = ? AND source = ?
                """,
                (athlete_id, source),
            ).fetchone()
        return self._subscription_from_row(row) if row is not None else None

    def list_subscriptions(self, athlete_id: str | None = None) -> list[SyncSubscription]:
        query = "SELECT * FROM sync_subscriptions"
        parameters: tuple[object, ...] = ()
        if athlete_id is not None:
            query += " WHERE athlete_id = ?"
            parameters = (athlete_id,)
        query += " ORDER BY athlete_id, source"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._subscription_from_row(row) for row in rows]

    def due_subscriptions(self, now: datetime) -> list[SyncSubscription]:
        due_at = self._datetime_text(now, "now")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM sync_subscriptions
                WHERE enabled = 1 AND next_run_at <= ?
                ORDER BY next_run_at, athlete_id, source
                """,
                (due_at,),
            ).fetchall()
        return [self._subscription_from_row(row) for row in rows]

    def claim_due(
        self,
        now: datetime,
        *,
        reason: str = "schedule",
        limit: int = 100,
        owner: str | None = None,
        lease_duration: timedelta = timedelta(minutes=15),
        exclude: Collection[tuple[str, str]] = (),
        lease_clock: Callable[[], datetime] | None = None,
    ) -> list[tuple[SyncSubscription, SyncRun]]:
        fallback_now = self._datetime(now, "now")
        self._lease_expiry(fallback_now, lease_duration)
        if not reason.strip():
            raise ValueError("reason must not be blank")
        if owner is not None and not owner.strip():
            raise ValueError("owner must not be blank")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        excluded = tuple(sorted(set(exclude)))
        exclusion_sql = "".join(
            " AND NOT (sync_subscriptions.athlete_id = ? AND sync_subscriptions.source = ?)"
            for _ in excluded
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            claimed_at = self._lease_time(lease_clock, fallback_now, "now")
            lease_expires_at = self._lease_expiry(claimed_at, lease_duration)
            parameters: list[object] = [claimed_at.isoformat()]
            for athlete_id, source in excluded:
                parameters.extend((athlete_id, source))
            parameters.append(limit)
            rows = connection.execute(
                f"""
                SELECT * FROM sync_subscriptions
                WHERE enabled = 1 AND next_run_at <= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM automation_sync_runs AS active_run
                      WHERE active_run.athlete_id = sync_subscriptions.athlete_id
                        AND active_run.source = sync_subscriptions.source
                        AND active_run.status = 'running'
                  )
                  {exclusion_sql}
                ORDER BY next_run_at, athlete_id, source
                LIMIT ?
                """,
                tuple(parameters),
            ).fetchall()
            claims: list[tuple[SyncSubscription, SyncRun]] = []
            for row in rows:
                subscription = self._subscription_from_row(row)
                scheduled_at = datetime.fromisoformat(row["next_run_at"])
                request_parts = [
                    subscription.athlete_id,
                    subscription.source,
                    scheduled_at.isoformat(),
                ]
                if reason != "schedule":
                    request_parts.append(reason)
                request_key = self._identifier("sync-request", *request_parts)
                run_row = connection.execute(
                    "SELECT * FROM automation_sync_runs WHERE request_key = ?",
                    (request_key,),
                ).fetchone()
                if run_row is None:
                    run = SyncRun(
                        run_id=self._identifier("sync-run", request_key),
                        athlete_id=subscription.athlete_id,
                        source=subscription.source,
                        reason=reason,
                        status="running",
                        started_at=claimed_at,
                        request_key=request_key,
                        lease_token=uuid4().hex,
                        lease_owner=owner,
                        lease_expires_at=lease_expires_at,
                        updated_at=claimed_at,
                    )
                    self._insert_sync_run(connection, run)
                else:
                    run = self._sync_run_from_row(run_row)
                    if (
                        run.athlete_id != subscription.athlete_id
                        or run.source != subscription.source
                        or run.reason != reason
                    ):
                        raise ValueError("sync request idempotency conflict")
                next_run_at = scheduled_at
                interval = timedelta(minutes=subscription.interval_minutes)
                while next_run_at <= claimed_at:
                    next_run_at += interval
                connection.execute(
                    """
                    UPDATE sync_subscriptions
                    SET next_run_at = ?
                    WHERE athlete_id = ? AND source = ? AND next_run_at = ?
                    """,
                    (
                        next_run_at.isoformat(),
                        subscription.athlete_id,
                        subscription.source,
                        scheduled_at.isoformat(),
                    ),
                )
                if run.status == "running":
                    claims.append((subscription, run))
            commit_at = self._lease_time(lease_clock, fallback_now, "now")
            if claims and lease_expires_at <= commit_at:
                raise ValueError("claimed sync run lease expired before commit")
            connection.commit()
            return claims
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def claim_force(
        self,
        now: datetime,
        *,
        limit: int = 100,
        owner: str | None = None,
        lease_duration: timedelta = timedelta(minutes=15),
        exclude: Collection[tuple[str, str]] = (),
        lease_clock: Callable[[], datetime] | None = None,
    ) -> list[tuple[SyncSubscription, SyncRun]]:
        """Atomically claim enabled work for the current forced minute slot."""

        fallback_now = self._datetime(now, "now")
        self._lease_expiry(fallback_now, lease_duration)
        if owner is not None and not owner.strip():
            raise ValueError("owner must not be blank")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        excluded = tuple(sorted(set(exclude)))
        exclusion_sql = "".join(
            " AND NOT (sync_subscriptions.athlete_id = ? AND sync_subscriptions.source = ?)"
            for _ in excluded
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            claimed_at = self._lease_time(lease_clock, fallback_now, "now")
            force_slot = claimed_at.replace(second=0, microsecond=0)
            lease_expires_at = self._lease_expiry(claimed_at, lease_duration)
            parameters: list[object] = []
            for athlete_id, source in excluded:
                parameters.extend((athlete_id, source))
            parameters.append(limit)
            rows = connection.execute(
                f"""
                SELECT * FROM sync_subscriptions
                WHERE enabled = 1
                  AND NOT EXISTS (
                      SELECT 1 FROM automation_sync_runs AS active_run
                      WHERE active_run.athlete_id = sync_subscriptions.athlete_id
                        AND active_run.source = sync_subscriptions.source
                        AND active_run.status = 'running'
                  )
                  {exclusion_sql}
                ORDER BY next_run_at, athlete_id, source
                LIMIT ?
                """,
                tuple(parameters),
            ).fetchall()
            claims: list[tuple[SyncSubscription, SyncRun]] = []
            for row in rows:
                subscription = self._subscription_from_row(row)
                scheduled_at = datetime.fromisoformat(row["next_run_at"])
                request_key = self._identifier(
                    "sync-request",
                    subscription.athlete_id,
                    subscription.source,
                    force_slot.isoformat(),
                    "force",
                )
                run_row = connection.execute(
                    "SELECT * FROM automation_sync_runs WHERE request_key = ?",
                    (request_key,),
                ).fetchone()
                if run_row is None:
                    run = SyncRun(
                        run_id=self._identifier("sync-run", request_key),
                        athlete_id=subscription.athlete_id,
                        source=subscription.source,
                        reason="force",
                        status="running",
                        started_at=claimed_at,
                        request_key=request_key,
                        lease_token=uuid4().hex,
                        lease_owner=owner,
                        lease_expires_at=lease_expires_at,
                        updated_at=claimed_at,
                    )
                    self._insert_sync_run(connection, run)
                else:
                    run = self._sync_run_from_row(run_row)
                    if (
                        run.athlete_id != subscription.athlete_id
                        or run.source != subscription.source
                        or run.reason != "force"
                    ):
                        raise ValueError("sync request idempotency conflict")

                if scheduled_at <= claimed_at:
                    next_run_at = scheduled_at
                    interval = timedelta(minutes=subscription.interval_minutes)
                    while next_run_at <= claimed_at:
                        next_run_at += interval
                    connection.execute(
                        """
                        UPDATE sync_subscriptions
                        SET next_run_at = ?
                        WHERE athlete_id = ? AND source = ? AND next_run_at = ?
                        """,
                        (
                            next_run_at.isoformat(),
                            subscription.athlete_id,
                            subscription.source,
                            scheduled_at.isoformat(),
                        ),
                    )
                if run.status == "running":
                    claims.append((subscription, run))
            commit_at = self._lease_time(lease_clock, fallback_now, "now")
            if claims and lease_expires_at <= commit_at:
                raise ValueError("claimed sync run lease expired before commit")
            connection.commit()
            return claims
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def start_sync_run(
        self,
        subscription: SyncSubscription,
        *,
        now: datetime,
        reason: str,
        request_key: str | None = None,
        owner: str | None = None,
        lease_duration: timedelta = timedelta(minutes=15),
    ) -> SyncRun:
        started_at = self._datetime(now, "now")
        if not reason.strip():
            raise ValueError("reason must not be blank")
        if request_key is None and reason == "schedule":
            raise ValueError("scheduled syncs must use claim_due or provide a stable request_key")
        if request_key is not None and not request_key.strip():
            raise ValueError("request_key must not be blank")
        if owner is not None and not owner.strip():
            raise ValueError("owner must not be blank")
        is_leased = reason == "schedule" or owner is not None
        lease_expires_at = self._lease_expiry(started_at, lease_duration) if is_leased else None
        run = SyncRun(
            run_id=self._identifier("sync-run", request_key) if request_key else uuid4().hex,
            athlete_id=subscription.athlete_id,
            source=subscription.source,
            reason=reason,
            status="running",
            started_at=started_at,
            request_key=request_key,
            lease_token=uuid4().hex if is_leased else None,
            lease_owner=owner,
            lease_expires_at=lease_expires_at,
            updated_at=started_at,
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if request_key is not None:
                row = connection.execute(
                    "SELECT * FROM automation_sync_runs WHERE request_key = ?",
                    (request_key,),
                ).fetchone()
                if row is not None:
                    existing = self._sync_run_from_row(row)
                    if (
                        existing.athlete_id != run.athlete_id
                        or existing.source != run.source
                        or existing.reason != run.reason
                    ):
                        raise ValueError("sync request idempotency conflict")
                    connection.commit()
                    return existing
            self._insert_sync_run(connection, run)
            connection.commit()
            return run
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _insert_sync_run(connection: sqlite3.Connection, run: SyncRun) -> None:
        connection.execute(
            """
            INSERT INTO automation_sync_runs (
                run_id, athlete_id, source, reason, status, started_at,
                completed_at, error, request_key, lease_token, lease_owner,
                lease_expires_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.run_id,
                run.athlete_id,
                run.source,
                run.reason,
                run.status,
                run.started_at.isoformat(),
                run.completed_at.isoformat() if run.completed_at is not None else None,
                run.error,
                run.request_key,
                run.lease_token,
                run.lease_owner,
                run.lease_expires_at.isoformat() if run.lease_expires_at is not None else None,
                run.updated_at.isoformat() if run.updated_at is not None else None,
            ),
        )

    def complete_sync_run(
        self,
        run_id: str,
        *,
        completed_at: datetime,
        lease_token: str | None = None,
        lease_clock: Callable[[], datetime] | None = None,
    ) -> SyncRun:
        return self._finish_sync_run(
            run_id,
            status="completed",
            completed_at=completed_at,
            error=None,
            lease_token=lease_token,
            lease_clock=lease_clock,
        )

    def fail_sync_run(
        self,
        run_id: str,
        *,
        error: str,
        completed_at: datetime,
        lease_token: str | None = None,
        lease_clock: Callable[[], datetime] | None = None,
    ) -> SyncRun:
        if not error.strip():
            raise ValueError("error must not be blank")
        return self._finish_sync_run(
            run_id,
            status="failed",
            completed_at=completed_at,
            error=error,
            lease_token=lease_token,
            lease_clock=lease_clock,
        )

    def _finish_sync_run(
        self,
        run_id: str,
        *,
        status: str,
        completed_at: datetime,
        error: str | None,
        lease_token: str | None,
        lease_clock: Callable[[], datetime] | None,
    ) -> SyncRun:
        completed = self._datetime(completed_at, "completed_at")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM automation_sync_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            existing = self._sync_run_from_row(row)
            if existing.status == "running":
                if existing.lease_token != lease_token:
                    raise ValueError("sync run lease token conflict")
                if existing.lease_token is not None:
                    checked_at = self._lease_time(lease_clock, completed, "completed_at")
                    if existing.lease_expires_at is None or existing.lease_expires_at <= checked_at:
                        raise ValueError("sync run lease has expired")
                assert existing.updated_at is not None
                if completed < existing.updated_at:
                    raise ValueError("completed_at must not be earlier than updated_at")
            candidate = SyncRun(
                run_id=existing.run_id,
                athlete_id=existing.athlete_id,
                source=existing.source,
                reason=existing.reason,
                status=status,
                started_at=existing.started_at,
                completed_at=completed,
                error=error,
                request_key=existing.request_key,
                updated_at=completed,
            )
            if existing.status != "running":
                if existing == candidate:
                    connection.commit()
                    return existing
                raise ValueError("sync run terminal state conflict")
            cursor = connection.execute(
                """
                UPDATE automation_sync_runs
                SET status = ?, completed_at = ?, error = ?,
                    lease_token = NULL, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE run_id = ? AND status = 'running'
                  AND (
                      (lease_token IS NULL AND ? IS NULL)
                      OR lease_token = ?
                  )
                """,
                (
                    status,
                    completed.isoformat(),
                    error,
                    completed.isoformat(),
                    run_id,
                    lease_token,
                    lease_token,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("sync run terminal state conflict")
            row = connection.execute(
                "SELECT * FROM automation_sync_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            assert row is not None
            result = self._sync_run_from_row(row)
            if existing.lease_token is not None:
                commit_at = self._lease_time(lease_clock, completed, "completed_at")
                if existing.lease_expires_at is None or existing.lease_expires_at <= commit_at:
                    raise ValueError("sync run lease expired before commit")
            connection.commit()
            return result
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def get_sync_run(self, run_id: str) -> SyncRun | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM automation_sync_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return self._sync_run_from_row(row) if row is not None else None

    @classmethod
    def _validate_owned_lease(
        cls,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        lease_token: str,
        owner: str,
        now: datetime,
    ) -> SyncRun:
        row = connection.execute(
            "SELECT * FROM automation_sync_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        run = cls._sync_run_from_row(row)
        if (
            run.status != "running"
            or run.lease_token != lease_token
            or run.lease_owner != owner
            or run.lease_expires_at is None
            or run.lease_expires_at <= now
        ):
            raise ValueError("automatic sync run lease is no longer owned")
        return run

    def validate_lease(
        self,
        run_id: str,
        *,
        lease_token: str,
        owner: str,
        now: datetime,
        connection: sqlite3.Connection | None = None,
    ) -> SyncRun:
        """Return the current run only while this active lease is still owned."""

        checked_at = self._datetime(now, "now")
        if not lease_token.strip():
            raise ValueError("lease token must not be blank")
        if not owner.strip():
            raise ValueError("lease owner must not be blank")
        if connection is not None:
            return self._validate_owned_lease(
                connection,
                run_id,
                lease_token=lease_token,
                owner=owner,
                now=checked_at,
            )
        with self._connection() as owned_connection:
            return self._validate_owned_lease(
                owned_connection,
                run_id,
                lease_token=lease_token,
                owner=owner,
                now=checked_at,
            )

    @contextmanager
    def lease_fenced_transaction(
        self,
        run_id: str,
        *,
        lease_token: str,
        owner: str,
        now: datetime,
        lease_clock: Callable[[], datetime] | None = None,
    ) -> Iterator[sqlite3.Connection]:
        """Hold the SQLite write lock while lease-owned ingestion is committed."""

        fallback_now = self._datetime(now, "now")
        if not lease_token.strip():
            raise ValueError("lease token must not be blank")
        if not owner.strip():
            raise ValueError("lease owner must not be blank")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._validate_owned_lease(
                connection,
                run_id,
                lease_token=lease_token,
                owner=owner,
                now=self._lease_time(lease_clock, fallback_now, "now"),
            )
            yield connection
            self._validate_owned_lease(
                connection,
                run_id,
                lease_token=lease_token,
                owner=owner,
                now=self._lease_time(lease_clock, fallback_now, "now"),
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def latest_sync_run(self, athlete_id: str, source: str) -> SyncRun | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM automation_sync_runs
                WHERE athlete_id = ? AND source = ?
                ORDER BY started_at DESC, run_id DESC
                LIMIT 1
                """,
                (athlete_id, source),
            ).fetchone()
        return self._sync_run_from_row(row) if row is not None else None

    def list_recoverable_runs(self, now: datetime) -> list[SyncRun]:
        recoverable_at = self._datetime_text(now, "now")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM automation_sync_runs
                WHERE status = 'running' AND lease_expires_at <= ?
                ORDER BY lease_expires_at, started_at, run_id
                """,
                (recoverable_at,),
            ).fetchall()
        return [self._sync_run_from_row(row) for row in rows]

    def reclaim_sync_run(
        self,
        run_id: str,
        *,
        now: datetime,
        owner: str | None = None,
        lease_duration: timedelta = timedelta(minutes=15),
        lease_clock: Callable[[], datetime] | None = None,
    ) -> SyncRun:
        fallback_now = self._datetime(now, "now")
        self._lease_expiry(fallback_now, lease_duration)
        if owner is not None and not owner.strip():
            raise ValueError("owner must not be blank")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            reclaimed_at = self._lease_time(lease_clock, fallback_now, "now")
            lease_expires_at = self._lease_expiry(reclaimed_at, lease_duration)
            row = connection.execute(
                "SELECT * FROM automation_sync_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            existing = self._sync_run_from_row(row)
            if existing.status != "running":
                raise ValueError("only running sync runs can be reclaimed")
            if existing.lease_token is None:
                raise ValueError("unleased sync runs cannot be reclaimed")
            if existing.lease_expires_at is not None and existing.lease_expires_at > reclaimed_at:
                raise ValueError("sync run lease is still active")
            assert existing.updated_at is not None
            if reclaimed_at < existing.updated_at:
                raise ValueError("reclaim time must not be earlier than updated_at")
            lease_token = uuid4().hex
            cursor = connection.execute(
                """
                UPDATE automation_sync_runs
                SET lease_token = ?, lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE run_id = ? AND status = 'running'
                  AND lease_token = ? AND lease_expires_at <= ?
                """,
                (
                    lease_token,
                    owner,
                    lease_expires_at.isoformat(),
                    reclaimed_at.isoformat(),
                    run_id,
                    existing.lease_token,
                    reclaimed_at.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("sync run could not be reclaimed")
            row = connection.execute(
                "SELECT * FROM automation_sync_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            assert row is not None
            result = self._sync_run_from_row(row)
            commit_at = self._lease_time(lease_clock, fallback_now, "now")
            if lease_expires_at <= commit_at:
                raise ValueError("reclaimed sync run lease expired before commit")
            connection.commit()
            return result
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def renew_lease(
        self,
        run_id: str,
        lease_token: str,
        *,
        owner: str,
        now: datetime,
        lease_duration: timedelta = timedelta(minutes=15),
        lease_clock: Callable[[], datetime] | None = None,
    ) -> SyncRun:
        fallback_now = self._datetime(now, "now")
        self._lease_expiry(fallback_now, lease_duration)
        if not lease_token.strip():
            raise ValueError("lease token must not be blank")
        if not owner.strip():
            raise ValueError("lease owner must not be blank")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            renewed_at = self._lease_time(lease_clock, fallback_now, "now")
            lease_expires_at = self._lease_expiry(renewed_at, lease_duration)
            row = connection.execute(
                "SELECT * FROM automation_sync_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            existing = self._sync_run_from_row(row)
            if existing.status != "running" or existing.lease_token != lease_token:
                raise ValueError("sync run lease token conflict")
            if existing.lease_owner != owner:
                raise ValueError("sync run lease owner conflict")
            assert existing.updated_at is not None
            if renewed_at < existing.updated_at:
                raise ValueError("renewal time must not be earlier than updated_at")
            if existing.lease_expires_at is None or existing.lease_expires_at <= renewed_at:
                raise ValueError("sync run lease has expired")
            if lease_expires_at <= existing.lease_expires_at:
                raise ValueError("renewal must extend the sync run lease")
            cursor = connection.execute(
                """
                UPDATE automation_sync_runs
                SET lease_expires_at = ?, updated_at = ?
                WHERE run_id = ? AND status = 'running'
                  AND lease_token = ? AND lease_owner = ?
                  AND lease_expires_at > ? AND updated_at <= ?
                """,
                (
                    lease_expires_at.isoformat(),
                    renewed_at.isoformat(),
                    run_id,
                    lease_token,
                    owner,
                    renewed_at.isoformat(),
                    renewed_at.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("sync run lease renewal conflict")
            row = connection.execute(
                "SELECT * FROM automation_sync_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            assert row is not None
            result = self._sync_run_from_row(row)
            commit_at = self._lease_time(lease_clock, fallback_now, "now")
            if (
                existing.lease_expires_at is None
                or existing.lease_expires_at <= commit_at
                or lease_expires_at <= commit_at
            ):
                raise ValueError("sync run lease expired before renewal commit")
            connection.commit()
            return result
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def save_change_set(
        self,
        run_id: str,
        result: IngestionResult,
        *,
        created_at: datetime,
        lease_token: str | None = None,
        connection: sqlite3.Connection | None = None,
        lease_clock: Callable[[], datetime] | None = None,
        projection_record_ids: tuple[str, ...] | None = None,
    ) -> DataChangeSet:
        created = self._datetime(created_at, "created_at")
        if connection is not None:
            return self._save_change_set(
                connection,
                run_id,
                result,
                created=created,
                lease_token=lease_token,
                lease_clock=lease_clock,
                projection_record_ids=projection_record_ids,
            )
        owned_connection = self._connect()
        try:
            owned_connection.execute("BEGIN IMMEDIATE")
            change_set = self._save_change_set(
                owned_connection,
                run_id,
                result,
                created=created,
                lease_token=lease_token,
                lease_clock=lease_clock,
                projection_record_ids=projection_record_ids,
            )
            owned_connection.commit()
            return change_set
        except BaseException:
            if owned_connection.in_transaction:
                owned_connection.rollback()
            raise
        finally:
            owned_connection.close()

    def _save_change_set(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        result: IngestionResult,
        *,
        created: datetime,
        lease_token: str | None,
        lease_clock: Callable[[], datetime] | None,
        projection_record_ids: tuple[str, ...] | None,
    ) -> DataChangeSet:
        run_row = connection.execute(
            "SELECT * FROM automation_sync_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if run_row is None:
            raise KeyError(run_id)
        run = self._sync_run_from_row(run_row)
        change_set = DataChangeSet(
            change_set_id=self._identifier("change-set", run_id),
            run_id=run_id,
            athlete_id=run.athlete_id,
            source=run.source,
            record_ids=tuple(result.changed_record_ids),
            affected_dates=tuple(result.affected_dates),
            created_at=created,
            projection_record_ids=(
                (LEGACY_PROJECTION_UNAVAILABLE,)
                if projection_record_ids is None
                else projection_record_ids
            ),
        )
        row = connection.execute(
            "SELECT * FROM data_change_sets WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is not None:
            existing = self._change_set_from_row(row)
            if (
                existing.athlete_id != change_set.athlete_id
                or existing.source != change_set.source
                or existing.record_ids != change_set.record_ids
                or existing.affected_dates != change_set.affected_dates
                or existing.projection_record_ids != change_set.projection_record_ids
            ):
                raise ValueError("change set idempotency conflict")
            return existing
        if run.status != "running":
            raise ValueError("sync run must be running to create a change set")
        if run.lease_token != lease_token:
            raise ValueError("sync run lease token conflict")
        if run.lease_token is not None:
            checked_at = self._lease_time(lease_clock, created, "created_at")
            if run.lease_expires_at is None or run.lease_expires_at <= checked_at:
                raise ValueError("sync run lease has expired")
        connection.execute(
            """
            INSERT INTO data_change_sets (
                change_set_id, run_id, athlete_id, source, record_ids_json,
                affected_dates_json, projection_record_ids_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                change_set.change_set_id,
                change_set.run_id,
                change_set.athlete_id,
                change_set.source,
                json.dumps(change_set.record_ids, ensure_ascii=False),
                json.dumps([value.isoformat() for value in change_set.affected_dates]),
                json.dumps(change_set.projection_record_ids, ensure_ascii=False),
                change_set.created_at.isoformat(),
            ),
        )
        if run.lease_token is not None:
            commit_at = self._lease_time(lease_clock, created, "created_at")
            if run.lease_expires_at is None or run.lease_expires_at <= commit_at:
                raise ValueError("sync run lease expired before change set commit")
        return change_set

    def _validate_effect_lease(
        self,
        connection: sqlite3.Connection,
        derived_run_id: str,
        *,
        run_id: str | None,
        lease_token: str | None,
        lease_owner: str | None,
        lease_checked_at: datetime | None,
        lease_clock: Callable[[], datetime] | None,
    ) -> SyncRun:
        row = connection.execute(
            "SELECT * FROM automation_sync_runs WHERE run_id = ?",
            (derived_run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(derived_run_id)
        run = self._sync_run_from_row(row)
        if run.status != "running":
            raise ValueError("sync run must be running to create an effect")
        if run.lease_token is None:
            if run_id is not None and run_id != derived_run_id:
                raise ValueError("effect sync run conflict")
            return run
        if (
            run_id != derived_run_id
            or lease_token != run.lease_token
            or lease_owner != run.lease_owner
            or (lease_checked_at is None and lease_clock is None)
        ):
            raise ValueError("sync run lease conflict")
        checked_at = self._lease_time(lease_clock, lease_checked_at, "lease_checked_at")
        if run.lease_expires_at is None or run.lease_expires_at <= checked_at:
            raise ValueError("sync run lease has expired")
        return run

    def _run_id_for_change_set(
        self,
        connection: sqlite3.Connection,
        change_set: DataChangeSet,
    ) -> str:
        row = connection.execute(
            """
            SELECT run_id, athlete_id, source
            FROM data_change_sets
            WHERE change_set_id = ?
            """,
            (change_set.change_set_id,),
        ).fetchone()
        if row is None:
            raise KeyError(change_set.change_set_id)
        if (
            row["run_id"] != change_set.run_id
            or row["athlete_id"] != change_set.athlete_id
            or row["source"] != change_set.source
        ):
            raise ValueError("change set identity conflict")
        return cast(str, row["run_id"])

    def save_snapshot(
        self,
        change_set: DataChangeSet,
        report_payload: dict[str, JsonValue],
        risk_level: str,
        *,
        created_at: datetime,
        analysis_kind: str = "wellness",
        algorithm_version: str = "wellness-report/1.1",
        run_id: str | None = None,
        lease_token: str | None = None,
        lease_owner: str | None = None,
        lease_checked_at: datetime | None = None,
        lease_clock: Callable[[], datetime] | None = None,
    ) -> AnalysisSnapshot:
        period = report_payload.get("period")
        if not isinstance(period, str) or not period.strip():
            raise ValueError("report_payload must contain a non-blank period")
        created = self._datetime(created_at, "created_at")
        snapshot = AnalysisSnapshot(
            snapshot_id=self._identifier(
                "snapshot",
                change_set.change_set_id,
                period,
                analysis_kind,
                algorithm_version,
            ),
            change_set_id=change_set.change_set_id,
            athlete_id=change_set.athlete_id,
            period=period,
            report=report_payload,
            risk_level=risk_level,
            created_at=created,
            analysis_kind=analysis_kind,
            algorithm_version=algorithm_version,
        )
        report_json = snapshot.report_json
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM analysis_snapshots
                WHERE change_set_id = ? AND period = ?
                  AND analysis_kind = ? AND algorithm_version = ?
                """,
                (
                    change_set.change_set_id,
                    period,
                    analysis_kind,
                    algorithm_version,
                ),
            ).fetchone()
            if row is not None:
                existing = self._snapshot_from_row(row)
                if (
                    existing.athlete_id != snapshot.athlete_id
                    or existing.risk_level != snapshot.risk_level
                    or existing.report_json != report_json
                ):
                    raise ValueError("analysis snapshot idempotency conflict")
                connection.commit()
                return existing
            derived_run_id = self._run_id_for_change_set(connection, change_set)
            self._validate_effect_lease(
                connection,
                derived_run_id,
                run_id=run_id,
                lease_token=lease_token,
                lease_owner=lease_owner,
                lease_checked_at=lease_checked_at,
                lease_clock=lease_clock,
            )
            connection.execute(
                """
                INSERT INTO analysis_snapshots (
                    snapshot_id, change_set_id, athlete_id, period,
                    analysis_kind, algorithm_version, report_json,
                    risk_level, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.change_set_id,
                    snapshot.athlete_id,
                    snapshot.period,
                    snapshot.analysis_kind,
                    snapshot.algorithm_version,
                    report_json,
                    snapshot.risk_level,
                    snapshot.created_at.isoformat(),
                ),
            )
            self._validate_effect_lease(
                connection,
                derived_run_id,
                run_id=run_id,
                lease_token=lease_token,
                lease_owner=lease_owner,
                lease_checked_at=lease_checked_at,
                lease_clock=lease_clock,
            )
            connection.commit()
            return snapshot
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def get_change_set_for_run(self, run_id: str) -> DataChangeSet | None:
        """Return the durable handoff for a sync run, if it has been published."""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM data_change_sets WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return self._change_set_from_row(row) if row is not None else None

    def list_snapshots(self, athlete_id: str) -> list[AnalysisSnapshot]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM analysis_snapshots
                WHERE athlete_id = ?
                ORDER BY created_at, analysis_kind, algorithm_version, snapshot_id
                """,
                (athlete_id,),
            ).fetchall()
        return [self._snapshot_from_row(row) for row in rows]

    def list_snapshots_for_change_set(self, change_set_id: str) -> list[AnalysisSnapshot]:
        """Return already-persisted analysis effects for recovery."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM analysis_snapshots
                WHERE change_set_id = ?
                ORDER BY created_at, analysis_kind, period, algorithm_version, snapshot_id
                """,
                (change_set_id,),
            ).fetchall()
        return [self._snapshot_from_row(row) for row in rows]

    def enqueue_notification(
        self,
        snapshot: AnalysisSnapshot,
        *,
        priority: str,
        deliver_after: datetime | None,
        title: str | None = None,
        body: str | None = None,
        created_at: datetime | None = None,
        policy_version: str = "notification-policy/1.0",
        channel: str = "inbox",
        run_id: str | None = None,
        lease_token: str | None = None,
        lease_owner: str | None = None,
        lease_checked_at: datetime | None = None,
        lease_clock: Callable[[], datetime] | None = None,
    ) -> UserNotification | None:
        created = self._datetime(created_at or datetime.now(UTC), "created_at")
        deliver_at = (
            self._datetime(deliver_after, "deliver_after") if deliver_after is not None else None
        )
        notification = UserNotification(
            notification_id=self._identifier(
                "notification",
                snapshot.snapshot_id,
                policy_version,
                channel,
            ),
            snapshot_id=snapshot.snapshot_id,
            athlete_id=snapshot.athlete_id,
            priority=priority,
            title=title or "Training analysis ready",
            body=body or f"Analysis for {snapshot.period} is ready ({snapshot.risk_level} risk).",
            deliver_after=deliver_at,
            created_at=created,
            policy_version=policy_version,
            channel=channel,
            next_attempt_at=deliver_at or created,
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM notification_inbox
                WHERE snapshot_id = ? AND policy_version = ? AND channel = ?
                """,
                (snapshot.snapshot_id, policy_version, channel),
            ).fetchone()
            if row is not None:
                existing = self._notification_from_row(row)
                if (
                    existing.athlete_id != notification.athlete_id
                    or existing.priority != notification.priority
                    or existing.title != notification.title
                    or existing.body != notification.body
                    or existing.deliver_after != notification.deliver_after
                ):
                    raise ValueError("notification idempotency conflict")
                connection.commit()
                return existing
            relation = connection.execute(
                """
                SELECT analysis_snapshots.athlete_id, data_change_sets.run_id,
                       sync_subscriptions.notification_mode AS current_notification_mode
                FROM analysis_snapshots
                JOIN data_change_sets
                  ON data_change_sets.change_set_id = analysis_snapshots.change_set_id
                LEFT JOIN sync_subscriptions
                  ON sync_subscriptions.athlete_id = data_change_sets.athlete_id
                 AND sync_subscriptions.source = data_change_sets.source
                WHERE analysis_snapshots.snapshot_id = ?
                """,
                (snapshot.snapshot_id,),
            ).fetchone()
            if relation is None:
                raise KeyError(snapshot.snapshot_id)
            if relation["athlete_id"] != snapshot.athlete_id:
                raise ValueError("analysis snapshot identity conflict")
            derived_run_id = cast(str, relation["run_id"])
            self._validate_effect_lease(
                connection,
                derived_run_id,
                run_id=run_id,
                lease_token=lease_token,
                lease_owner=lease_owner,
                lease_checked_at=lease_checked_at,
                lease_clock=lease_clock,
            )
            if relation["current_notification_mode"] == NotificationMode.OFF.value:
                connection.commit()
                return None
            connection.execute(
                """
                INSERT INTO notification_inbox (
                    notification_id, snapshot_id, athlete_id, policy_version,
                    channel, priority, title, body, deliver_after, created_at,
                    read_at, delivered_at,
                    delivery_attempts, last_error, next_attempt_at,
                    delivery_lease_token, delivery_lease_owner, delivery_lease_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, NULL, ?, NULL, NULL, NULL)
                """,
                (
                    notification.notification_id,
                    notification.snapshot_id,
                    notification.athlete_id,
                    notification.policy_version,
                    notification.channel,
                    notification.priority,
                    notification.title,
                    notification.body,
                    notification.deliver_after.isoformat()
                    if notification.deliver_after is not None
                    else None,
                    notification.created_at.isoformat(),
                    notification.next_attempt_at.isoformat()
                    if notification.next_attempt_at is not None
                    else notification.created_at.isoformat(),
                ),
            )
            self._validate_effect_lease(
                connection,
                derived_run_id,
                run_id=run_id,
                lease_token=lease_token,
                lease_owner=lease_owner,
                lease_checked_at=lease_checked_at,
                lease_clock=lease_clock,
            )
            connection.commit()
            return notification
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def list_notifications(
        self,
        athlete_id: str,
        *,
        include_read: bool = False,
    ) -> list[UserNotification]:
        query = "SELECT * FROM notification_inbox WHERE athlete_id = ?"
        if not include_read:
            query += " AND read_at IS NULL"
        query += " ORDER BY created_at, notification_id"
        with self._connection() as connection:
            rows = connection.execute(query, (athlete_id,)).fetchall()
        return [self._notification_from_row(row) for row in rows]

    def list_notifications_for_change_set(
        self,
        change_set_id: str,
    ) -> list[UserNotification]:
        """Return notifications already derived from a durable change set."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT notification_inbox.*
                FROM notification_inbox
                JOIN analysis_snapshots
                  ON analysis_snapshots.snapshot_id = notification_inbox.snapshot_id
                WHERE analysis_snapshots.change_set_id = ?
                ORDER BY notification_inbox.created_at, notification_inbox.notification_id
                """,
                (change_set_id,),
            ).fetchall()
        return [self._notification_from_row(row) for row in rows]

    def list_unread(self, athlete_id: str) -> list[UserNotification]:
        return self.list_notifications(athlete_id)

    def list_pending(self, now: datetime) -> list[UserNotification]:
        due_at = self._datetime_text(now, "now")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM notification_inbox
                WHERE delivered_at IS NULL
                  AND next_attempt_at <= ?
                  AND (
                      delivery_lease_token IS NULL
                      OR delivery_lease_expires_at <= ?
                  )
                ORDER BY next_attempt_at, created_at, notification_id
                """,
                (due_at, due_at),
            ).fetchall()
        return [self._notification_from_row(row) for row in rows]

    def claim_pending(
        self,
        now: datetime,
        *,
        owner: str,
        lease_duration: timedelta = timedelta(minutes=5),
        limit: int = 100,
        lease_clock: Callable[[], datetime] | None = None,
    ) -> list[UserNotification]:
        """Atomically lease a bounded, fair batch of due notification deliveries."""

        fallback_now = self._datetime(now, "now")
        self._lease_expiry(fallback_now, lease_duration)
        if not owner.strip():
            raise ValueError("owner must not be blank")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            claimed_at = self._lease_time(lease_clock, fallback_now, "now")
            lease_expires_at = self._lease_expiry(claimed_at, lease_duration)
            rows = connection.execute(
                """
                SELECT notification_inbox.notification_id
                FROM notification_inbox
                JOIN analysis_snapshots
                  ON analysis_snapshots.snapshot_id = notification_inbox.snapshot_id
                JOIN data_change_sets
                  ON data_change_sets.change_set_id = analysis_snapshots.change_set_id
                LEFT JOIN sync_subscriptions
                  ON sync_subscriptions.athlete_id = data_change_sets.athlete_id
                 AND sync_subscriptions.source = data_change_sets.source
                WHERE notification_inbox.delivered_at IS NULL
                  AND notification_inbox.next_attempt_at <= ?
                  AND (
                      notification_inbox.delivery_lease_token IS NULL
                      OR notification_inbox.delivery_lease_expires_at <= ?
                  )
                  AND (
                      sync_subscriptions.notification_mode IS NULL
                      OR sync_subscriptions.notification_mode != ?
                  )
                ORDER BY notification_inbox.next_attempt_at,
                         notification_inbox.created_at,
                         notification_inbox.notification_id
                LIMIT ?
                """,
                (
                    claimed_at.isoformat(),
                    claimed_at.isoformat(),
                    NotificationMode.OFF.value,
                    limit,
                ),
            ).fetchall()
            claimed: list[UserNotification] = []
            for row in rows:
                token = uuid4().hex
                cursor = connection.execute(
                    """
                    UPDATE notification_inbox
                    SET delivery_lease_token = ?, delivery_lease_owner = ?,
                        delivery_lease_expires_at = ?
                    WHERE notification_id = ? AND delivered_at IS NULL
                      AND next_attempt_at <= ?
                      AND (
                          delivery_lease_token IS NULL
                          OR delivery_lease_expires_at <= ?
                      )
                    """,
                    (
                        token,
                        owner,
                        lease_expires_at.isoformat(),
                        row["notification_id"],
                        claimed_at.isoformat(),
                        claimed_at.isoformat(),
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("notification delivery claim conflict")
                claimed_row = connection.execute(
                    "SELECT * FROM notification_inbox WHERE notification_id = ?",
                    (row["notification_id"],),
                ).fetchone()
                assert claimed_row is not None
                claimed.append(self._notification_from_row(claimed_row))
            commit_at = self._lease_time(lease_clock, fallback_now, "now")
            if claimed and lease_expires_at <= commit_at:
                raise ValueError("notification delivery claim expired before commit")
            connection.commit()
            return claimed
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def renew_delivery_claim(
        self,
        notification_id: str,
        *,
        lease_token: str,
        owner: str,
        now: datetime,
        lease_duration: timedelta = timedelta(minutes=5),
        lease_clock: Callable[[], datetime] | None = None,
    ) -> UserNotification:
        """Validate and refresh one claim immediately before external delivery."""

        fallback_now = self._datetime(now, "now")
        self._lease_expiry(fallback_now, lease_duration)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            renewed_at = self._lease_time(lease_clock, fallback_now, "now")
            row = connection.execute(
                """
                SELECT notification_inbox.*,
                       sync_subscriptions.notification_mode AS current_notification_mode
                FROM notification_inbox
                JOIN analysis_snapshots
                  ON analysis_snapshots.snapshot_id = notification_inbox.snapshot_id
                JOIN data_change_sets
                  ON data_change_sets.change_set_id = analysis_snapshots.change_set_id
                LEFT JOIN sync_subscriptions
                  ON sync_subscriptions.athlete_id = data_change_sets.athlete_id
                 AND sync_subscriptions.source = data_change_sets.source
                WHERE notification_inbox.notification_id = ?
                """,
                (notification_id,),
            ).fetchone()
            if row is None:
                raise KeyError(notification_id)
            existing = self._notification_from_row(row)
            self._validate_delivery_claim(existing, lease_token, owner, renewed_at)
            if row["current_notification_mode"] == NotificationMode.OFF.value:
                cursor = connection.execute(
                    """
                    UPDATE notification_inbox
                    SET delivery_lease_token = NULL,
                        delivery_lease_owner = NULL,
                        delivery_lease_expires_at = NULL
                    WHERE notification_id = ? AND delivered_at IS NULL
                      AND delivery_lease_token = ? AND delivery_lease_owner = ?
                    """,
                    (notification_id, lease_token, owner),
                )
                if cursor.rowcount != 1:
                    raise ValueError("notification delivery lease conflict")
                connection.commit()
                raise ValueError("notification delivery suppressed by current policy")
            lease_expires_at = self._lease_expiry(renewed_at, lease_duration)
            cursor = connection.execute(
                """
                UPDATE notification_inbox
                SET delivery_lease_expires_at = ?
                WHERE notification_id = ? AND delivered_at IS NULL
                  AND delivery_lease_token = ? AND delivery_lease_owner = ?
                """,
                (
                    lease_expires_at.isoformat(),
                    notification_id,
                    lease_token,
                    owner,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("notification delivery lease conflict")
            row = connection.execute(
                "SELECT * FROM notification_inbox WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
            assert row is not None
            renewed = self._notification_from_row(row)
            commit_at = self._lease_time(lease_clock, fallback_now, "now")
            self._validate_delivery_claim(renewed, lease_token, owner, commit_at)
            connection.commit()
            return renewed
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def deliver_claimed(
        self,
        notification_id: str,
        deliver: Callable[[UserNotification], None],
        *,
        lease_token: str,
        owner: str,
        now: datetime,
        lease_clock: Callable[[], datetime] | None = None,
    ) -> UserNotification | None:
        """Fence policy, a bounded non-database callback, and its receipt atomically."""

        fallback_now = self._datetime(now, "now")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            checked_at = self._lease_time(lease_clock, fallback_now, "now")
            row = connection.execute(
                """
                SELECT notification_inbox.*,
                       sync_subscriptions.notification_mode AS current_notification_mode
                FROM notification_inbox
                JOIN analysis_snapshots
                  ON analysis_snapshots.snapshot_id = notification_inbox.snapshot_id
                JOIN data_change_sets
                  ON data_change_sets.change_set_id = analysis_snapshots.change_set_id
                LEFT JOIN sync_subscriptions
                  ON sync_subscriptions.athlete_id = data_change_sets.athlete_id
                 AND sync_subscriptions.source = data_change_sets.source
                WHERE notification_inbox.notification_id = ?
                """,
                (notification_id,),
            ).fetchone()
            if row is None:
                raise KeyError(notification_id)
            notification = self._notification_from_row(row)
            self._validate_delivery_claim(notification, lease_token, owner, checked_at)
            if row["current_notification_mode"] == NotificationMode.OFF.value:
                cursor = connection.execute(
                    """
                    UPDATE notification_inbox
                    SET delivery_lease_token = NULL,
                        delivery_lease_owner = NULL,
                        delivery_lease_expires_at = NULL
                    WHERE notification_id = ? AND delivered_at IS NULL
                      AND delivery_lease_token = ? AND delivery_lease_owner = ?
                    """,
                    (notification_id, lease_token, owner),
                )
                if cursor.rowcount != 1:
                    raise ValueError("notification delivery lease conflict")
                connection.commit()
                return None

            try:
                deliver(notification)
            except Exception as exc:
                failed_at = self._lease_time(lease_clock, fallback_now, "now")
                next_attempt = failed_at + self._delivery_backoff(
                    notification.delivery_attempts + 1
                )
                cursor = connection.execute(
                    """
                    UPDATE notification_inbox
                    SET delivery_attempts = delivery_attempts + 1,
                        last_error = ?, next_attempt_at = ?,
                        delivery_lease_token = NULL,
                        delivery_lease_owner = NULL,
                        delivery_lease_expires_at = NULL
                    WHERE notification_id = ? AND delivered_at IS NULL
                      AND delivery_lease_token = ? AND delivery_lease_owner = ?
                    """,
                    (
                        f"{type(exc).__name__}: delivery failed",
                        next_attempt.isoformat(),
                        notification_id,
                        lease_token,
                        owner,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("notification delivery lease conflict") from exc
                connection.commit()
                raise

            delivered_at = self._lease_time(lease_clock, fallback_now, "now")
            cursor = connection.execute(
                """
                UPDATE notification_inbox
                SET delivered_at = ?, delivery_attempts = delivery_attempts + 1,
                    last_error = NULL,
                    delivery_lease_token = NULL,
                    delivery_lease_owner = NULL,
                    delivery_lease_expires_at = NULL
                WHERE notification_id = ? AND delivered_at IS NULL
                  AND delivery_lease_token = ? AND delivery_lease_owner = ?
                """,
                (
                    delivered_at.isoformat(),
                    notification_id,
                    lease_token,
                    owner,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("notification delivery lease conflict")
            row = connection.execute(
                "SELECT * FROM notification_inbox WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
            assert row is not None
            delivered = self._notification_from_row(row)
            connection.commit()
            return delivered
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def get_notification(self, notification_id: str) -> UserNotification | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM notification_inbox WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
        return self._notification_from_row(row) if row is not None else None

    def mark_read(self, notification_id: str, *, read_at: datetime) -> UserNotification:
        read = self._datetime_text(read_at, "read_at")
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE notification_inbox
                SET read_at = COALESCE(read_at, ?)
                WHERE notification_id = ?
                """,
                (read, notification_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(notification_id)
            row = connection.execute(
                "SELECT * FROM notification_inbox WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
        assert row is not None
        return self._notification_from_row(row)

    def mark_delivered(
        self,
        notification_id: str,
        *,
        delivered_at: datetime,
        lease_token: str | None = None,
        owner: str | None = None,
    ) -> UserNotification:
        delivered = self._datetime(delivered_at, "delivered_at")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM notification_inbox WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
            if row is None:
                raise KeyError(notification_id)
            existing = self._notification_from_row(row)
            self._validate_delivery_claim(existing, lease_token, owner, delivered)
            cursor = connection.execute(
                """
                UPDATE notification_inbox
                SET delivered_at = COALESCE(delivered_at, ?),
                    delivery_attempts = delivery_attempts
                        + CASE WHEN delivered_at IS NULL THEN 1 ELSE 0 END,
                    last_error = NULL,
                    delivery_lease_token = NULL,
                    delivery_lease_owner = NULL,
                    delivery_lease_expires_at = NULL
                WHERE notification_id = ?
                """,
                (delivered.isoformat(), notification_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(notification_id)
            row = connection.execute(
                "SELECT * FROM notification_inbox WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
            assert row is not None
            result = self._notification_from_row(row)
            connection.commit()
            return result
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def mark_delivery_failed(
        self,
        notification_id: str,
        error: str,
        *,
        failed_at: datetime | None = None,
        lease_token: str | None = None,
        owner: str | None = None,
    ) -> UserNotification:
        if not error.strip():
            raise ValueError("error must not be blank")
        failure_time = self._datetime(failed_at or datetime.now(UTC), "failed_at")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM notification_inbox WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
            if row is None:
                raise KeyError(notification_id)
            existing = self._notification_from_row(row)
            self._validate_delivery_claim(existing, lease_token, owner, failure_time)
            next_attempt = failure_time + self._delivery_backoff(existing.delivery_attempts + 1)
            connection.execute(
                """
                UPDATE notification_inbox
                SET delivery_attempts = delivery_attempts + 1,
                    last_error = ?, next_attempt_at = ?,
                    delivery_lease_token = NULL,
                    delivery_lease_owner = NULL,
                    delivery_lease_expires_at = NULL
                WHERE notification_id = ? AND delivered_at IS NULL
                """,
                (error, next_attempt.isoformat(), notification_id),
            )
            row = connection.execute(
                "SELECT * FROM notification_inbox WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
            assert row is not None
            result = self._notification_from_row(row)
            connection.commit()
            return result
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _validate_delivery_claim(
        notification: UserNotification,
        lease_token: str | None,
        owner: str | None,
        checked_at: datetime,
    ) -> None:
        if notification.delivery_lease_token is None:
            if lease_token is not None or owner is not None:
                raise ValueError("notification delivery lease conflict")
            return
        if (
            lease_token != notification.delivery_lease_token
            or owner != notification.delivery_lease_owner
            or notification.delivery_lease_expires_at is None
            or notification.delivery_lease_expires_at <= checked_at
        ):
            raise ValueError("notification delivery lease conflict")

    @staticmethod
    def _delivery_backoff(attempt: int) -> timedelta:
        exponent = min(max(attempt - 1, 0), 6)
        return timedelta(seconds=min(3600, 60 * (2**exponent)))

    @staticmethod
    def _identifier(kind: str, *parts: str) -> str:
        encoded = json.dumps((kind, *parts), separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _datetime(value: datetime, field_name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must be timezone-aware")
        return value.astimezone(UTC)

    @classmethod
    def _datetime_text(cls, value: datetime, field_name: str) -> str:
        return cls._datetime(value, field_name).isoformat()

    @classmethod
    def _lease_time(
        cls,
        lease_clock: Callable[[], datetime] | None,
        fallback: datetime | None,
        field_name: str,
    ) -> datetime:
        """Read authoritative lease time while locked, with explicit-time compatibility."""

        if lease_clock is not None:
            return cls._datetime(lease_clock(), "lease_clock")
        if fallback is None:
            raise ValueError(f"{field_name} is required when lease_clock is not provided")
        return cls._datetime(fallback, field_name)

    @staticmethod
    def _lease_expiry(started_at: datetime, lease_duration: timedelta) -> datetime:
        if not isinstance(lease_duration, timedelta) or lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        return started_at + lease_duration

    @staticmethod
    def _subscription_from_row(row: sqlite3.Row) -> SyncSubscription:
        return SyncSubscription(
            athlete_id=row["athlete_id"],
            source=row["source"],
            interval_minutes=row["interval_minutes"],
            lookback_days=row["lookback_days"],
            notification_mode=NotificationMode(row["notification_mode"]),
            digest_hour_local=row["digest_hour_local"],
            enabled=bool(row["enabled"]),
        )

    @staticmethod
    def _sync_run_from_row(row: sqlite3.Row) -> SyncRun:
        return SyncRun(
            run_id=row["run_id"],
            athlete_id=row["athlete_id"],
            source=row["source"],
            reason=row["reason"],
            status=row["status"],
            started_at=datetime.fromisoformat(row["started_at"]),
            completed_at=datetime.fromisoformat(row["completed_at"])
            if row["completed_at"] is not None
            else None,
            error=row["error"],
            request_key=row["request_key"],
            lease_token=row["lease_token"],
            lease_owner=row["lease_owner"],
            lease_expires_at=datetime.fromisoformat(row["lease_expires_at"])
            if row["lease_expires_at"] is not None
            else None,
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _change_set_from_row(row: sqlite3.Row) -> DataChangeSet:
        return DataChangeSet(
            change_set_id=row["change_set_id"],
            run_id=row["run_id"],
            athlete_id=row["athlete_id"],
            source=row["source"],
            record_ids=tuple(json.loads(row["record_ids_json"])),
            affected_dates=tuple(
                date.fromisoformat(value) for value in json.loads(row["affected_dates_json"])
            ),
            created_at=datetime.fromisoformat(row["created_at"]),
            projection_record_ids=tuple(json.loads(row["projection_record_ids_json"])),
        )

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row) -> AnalysisSnapshot:
        return AnalysisSnapshot(
            snapshot_id=row["snapshot_id"],
            change_set_id=row["change_set_id"],
            athlete_id=row["athlete_id"],
            period=row["period"],
            report=cast(dict[str, JsonValue], json.loads(row["report_json"])),
            risk_level=row["risk_level"],
            created_at=datetime.fromisoformat(row["created_at"]),
            analysis_kind=row["analysis_kind"],
            algorithm_version=row["algorithm_version"],
        )

    @staticmethod
    def _notification_from_row(row: sqlite3.Row) -> UserNotification:
        return UserNotification(
            notification_id=row["notification_id"],
            snapshot_id=row["snapshot_id"],
            athlete_id=row["athlete_id"],
            priority=row["priority"],
            title=row["title"],
            body=row["body"],
            deliver_after=datetime.fromisoformat(row["deliver_after"])
            if row["deliver_after"] is not None
            else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            policy_version=row["policy_version"],
            channel=row["channel"],
            read_at=datetime.fromisoformat(row["read_at"]) if row["read_at"] is not None else None,
            delivered_at=datetime.fromisoformat(row["delivered_at"])
            if row["delivered_at"] is not None
            else None,
            delivery_attempts=row["delivery_attempts"],
            last_error=row["last_error"],
            next_attempt_at=datetime.fromisoformat(row["next_attempt_at"]),
            delivery_lease_token=row["delivery_lease_token"],
            delivery_lease_owner=row["delivery_lease_owner"],
            delivery_lease_expires_at=datetime.fromisoformat(row["delivery_lease_expires_at"])
            if row["delivery_lease_expires_at"] is not None
            else None,
        )
