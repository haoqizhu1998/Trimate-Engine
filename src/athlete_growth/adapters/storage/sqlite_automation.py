"""SQLite persistence for restart-safe automation state."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from athlete_growth.application.automation import (
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
        with self._connection() as connection:
            connection.executescript(
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
                );
                CREATE INDEX IF NOT EXISTS idx_sync_subscriptions_due
                    ON sync_subscriptions(enabled, next_run_at, athlete_id, source);

                CREATE TABLE IF NOT EXISTS automation_sync_runs (
                    run_id TEXT PRIMARY KEY,
                    athlete_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('running', 'completed', 'failed')),
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_automation_sync_runs_latest
                    ON automation_sync_runs(athlete_id, source, started_at DESC, run_id DESC);

                CREATE TABLE IF NOT EXISTS data_change_sets (
                    change_set_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL UNIQUE,
                    athlete_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    record_ids_json TEXT NOT NULL,
                    affected_dates_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES automation_sync_runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS analysis_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    change_set_id TEXT NOT NULL,
                    athlete_id TEXT NOT NULL,
                    period TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    risk_level TEXT NOT NULL
                        CHECK (risk_level IN ('unknown', 'low', 'moderate', 'high')),
                    created_at TEXT NOT NULL,
                    UNIQUE (change_set_id, period),
                    FOREIGN KEY (change_set_id) REFERENCES data_change_sets(change_set_id)
                );
                CREATE INDEX IF NOT EXISTS idx_analysis_snapshots_athlete
                    ON analysis_snapshots(athlete_id, created_at, snapshot_id);

                CREATE TABLE IF NOT EXISTS notification_inbox (
                    notification_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL UNIQUE,
                    athlete_id TEXT NOT NULL,
                    priority TEXT NOT NULL
                        CHECK (priority IN ('actionable', 'digest')),
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    deliver_after TEXT,
                    created_at TEXT NOT NULL,
                    read_at TEXT,
                    delivered_at TEXT,
                    delivery_attempts INTEGER NOT NULL DEFAULT 0
                        CHECK (delivery_attempts >= 0),
                    last_error TEXT,
                    FOREIGN KEY (snapshot_id) REFERENCES analysis_snapshots(snapshot_id)
                );
                CREATE INDEX IF NOT EXISTS idx_notification_inbox_unread
                    ON notification_inbox(athlete_id, read_at, created_at, notification_id);
                CREATE INDEX IF NOT EXISTS idx_notification_inbox_pending
                    ON notification_inbox(delivered_at, deliver_after, created_at, notification_id);
                """
            )
        os.chmod(self.database_file, 0o600)

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

    def start_sync_run(
        self,
        subscription: SyncSubscription,
        *,
        now: datetime,
        reason: str,
    ) -> SyncRun:
        started_at = self._datetime(now, "now")
        run = SyncRun(
            run_id=uuid4().hex,
            athlete_id=subscription.athlete_id,
            source=subscription.source,
            reason=reason,
            status="running",
            started_at=started_at,
        )
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO automation_sync_runs (
                    run_id, athlete_id, source, reason, status, started_at,
                    completed_at, error
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    run.run_id,
                    run.athlete_id,
                    run.source,
                    run.reason,
                    run.status,
                    run.started_at.isoformat(),
                ),
            )
        return run

    def complete_sync_run(self, run_id: str, *, completed_at: datetime) -> SyncRun:
        return self._finish_sync_run(
            run_id,
            status="completed",
            completed_at=completed_at,
            error=None,
        )

    def fail_sync_run(
        self,
        run_id: str,
        *,
        error: str,
        completed_at: datetime,
    ) -> SyncRun:
        if not error.strip():
            raise ValueError("error must not be blank")
        return self._finish_sync_run(
            run_id,
            status="failed",
            completed_at=completed_at,
            error=error,
        )

    def _finish_sync_run(
        self,
        run_id: str,
        *,
        status: str,
        completed_at: datetime,
        error: str | None,
    ) -> SyncRun:
        completed = self._datetime_text(completed_at, "completed_at")
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE automation_sync_runs
                SET status = ?, completed_at = ?, error = ?
                WHERE run_id = ?
                """,
                (status, completed, error, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(run_id)
            row = connection.execute(
                "SELECT * FROM automation_sync_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        assert row is not None
        return self._sync_run_from_row(row)

    def get_sync_run(self, run_id: str) -> SyncRun | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM automation_sync_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return self._sync_run_from_row(row) if row is not None else None

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

    def save_change_set(
        self,
        run_id: str,
        result: IngestionResult,
        *,
        created_at: datetime,
    ) -> DataChangeSet:
        run = self.get_sync_run(run_id)
        if run is None:
            raise KeyError(run_id)
        created = self._datetime(created_at, "created_at")
        change_set = DataChangeSet(
            change_set_id=self._identifier("change-set", run_id),
            run_id=run_id,
            athlete_id=run.athlete_id,
            source=run.source,
            record_ids=tuple(result.changed_record_ids),
            affected_dates=tuple(result.affected_dates),
            created_at=created,
        )
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO data_change_sets (
                    change_set_id, run_id, athlete_id, source, record_ids_json,
                    affected_dates_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    change_set.change_set_id,
                    change_set.run_id,
                    change_set.athlete_id,
                    change_set.source,
                    json.dumps(change_set.record_ids, ensure_ascii=False),
                    json.dumps([value.isoformat() for value in change_set.affected_dates]),
                    change_set.created_at.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM data_change_sets WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        assert row is not None
        return self._change_set_from_row(row)

    def save_snapshot(
        self,
        change_set: DataChangeSet,
        report_payload: dict[str, JsonValue],
        risk_level: str,
        *,
        created_at: datetime,
    ) -> AnalysisSnapshot:
        period = report_payload.get("period")
        if not isinstance(period, str) or not period.strip():
            raise ValueError("report_payload must contain a non-blank period")
        created = self._datetime(created_at, "created_at")
        snapshot = AnalysisSnapshot(
            snapshot_id=self._identifier("snapshot", change_set.change_set_id, period),
            change_set_id=change_set.change_set_id,
            athlete_id=change_set.athlete_id,
            period=period,
            report=report_payload,
            risk_level=risk_level,
            created_at=created,
        )
        report_json = json.dumps(
            snapshot.report,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO analysis_snapshots (
                    snapshot_id, change_set_id, athlete_id, period, report_json,
                    risk_level, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.change_set_id,
                    snapshot.athlete_id,
                    snapshot.period,
                    report_json,
                    snapshot.risk_level,
                    snapshot.created_at.isoformat(),
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM analysis_snapshots
                WHERE change_set_id = ? AND period = ?
                """,
                (change_set.change_set_id, period),
            ).fetchone()
        assert row is not None
        return self._snapshot_from_row(row)

    def list_snapshots(self, athlete_id: str) -> list[AnalysisSnapshot]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM analysis_snapshots
                WHERE athlete_id = ?
                ORDER BY created_at, snapshot_id
                """,
                (athlete_id,),
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
    ) -> UserNotification:
        created = self._datetime(created_at or datetime.now(UTC), "created_at")
        deliver_at = (
            self._datetime(deliver_after, "deliver_after") if deliver_after is not None else None
        )
        notification = UserNotification(
            notification_id=self._identifier("notification", snapshot.snapshot_id),
            snapshot_id=snapshot.snapshot_id,
            athlete_id=snapshot.athlete_id,
            priority=priority,
            title=title or "Training analysis ready",
            body=body or f"Analysis for {snapshot.period} is ready ({snapshot.risk_level} risk).",
            deliver_after=deliver_at,
            created_at=created,
        )
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO notification_inbox (
                    notification_id, snapshot_id, athlete_id, priority, title, body,
                    deliver_after, created_at, read_at, delivered_at,
                    delivery_attempts, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, NULL)
                """,
                (
                    notification.notification_id,
                    notification.snapshot_id,
                    notification.athlete_id,
                    notification.priority,
                    notification.title,
                    notification.body,
                    notification.deliver_after.isoformat()
                    if notification.deliver_after is not None
                    else None,
                    notification.created_at.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM notification_inbox WHERE snapshot_id = ?",
                (snapshot.snapshot_id,),
            ).fetchone()
        assert row is not None
        return self._notification_from_row(row)

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

    def list_unread(self, athlete_id: str) -> list[UserNotification]:
        return self.list_notifications(athlete_id)

    def list_pending(self, now: datetime) -> list[UserNotification]:
        due_at = self._datetime_text(now, "now")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM notification_inbox
                WHERE delivered_at IS NULL
                  AND (deliver_after IS NULL OR deliver_after <= ?)
                ORDER BY COALESCE(deliver_after, created_at), created_at, notification_id
                """,
                (due_at,),
            ).fetchall()
        return [self._notification_from_row(row) for row in rows]

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
    ) -> UserNotification:
        delivered = self._datetime_text(delivered_at, "delivered_at")
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE notification_inbox
                SET delivered_at = COALESCE(delivered_at, ?),
                    delivery_attempts = delivery_attempts
                        + CASE WHEN delivered_at IS NULL THEN 1 ELSE 0 END,
                    last_error = NULL
                WHERE notification_id = ?
                """,
                (delivered, notification_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(notification_id)
            row = connection.execute(
                "SELECT * FROM notification_inbox WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
        assert row is not None
        return self._notification_from_row(row)

    def mark_delivery_failed(self, notification_id: str, error: str) -> UserNotification:
        if not error.strip():
            raise ValueError("error must not be blank")
        with self._connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM notification_inbox WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(notification_id)
            connection.execute(
                """
                UPDATE notification_inbox
                SET delivery_attempts = delivery_attempts + 1,
                    last_error = ?
                WHERE notification_id = ? AND delivered_at IS NULL
                """,
                (error, notification_id),
            )
            row = connection.execute(
                "SELECT * FROM notification_inbox WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
        assert row is not None
        return self._notification_from_row(row)

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
            read_at=datetime.fromisoformat(row["read_at"]) if row["read_at"] is not None else None,
            delivered_at=datetime.fromisoformat(row["delivered_at"])
            if row["delivered_at"] is not None
            else None,
            delivery_attempts=row["delivery_attempts"],
            last_error=row["last_error"],
        )
