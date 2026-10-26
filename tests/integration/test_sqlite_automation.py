from __future__ import annotations

import gc
import hashlib
import json
import sqlite3
import subprocess
import sys
import unittest
import warnings
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event
from typing import Any, cast

from athlete_growth.adapters.storage.sqlite_automation import SQLiteAutomationRepository
from athlete_growth.adapters.storage.sqlite_profiles import SQLiteAthleteProfileRepository
from athlete_growth.adapters.storage.sqlite_records import SQLiteRecordStore
from athlete_growth.application.automation import (
    AnalysisSnapshot,
    AutomaticTrainingLoop,
    NotificationMode,
    SyncSubscription,
    UserNotification,
)
from athlete_growth.application.ingest.service import IngestionResult
from athlete_growth.application.report.service import ReportService
from athlete_growth.domain.athlete import AthleteProfile
from athlete_growth.domain.records import CanonicalRecord, EvidenceRef, JsonValue


def _seed_v1_database(database_file: Path, *, invalid_run: bool = False) -> None:
    completed_at = None if invalid_run else "2026-10-26T12:01:00+00:00"
    with closing(sqlite3.connect(database_file)) as connection:
        connection.executescript(
            """
            CREATE TABLE sync_subscriptions (
                athlete_id TEXT NOT NULL,
                source TEXT NOT NULL,
                interval_minutes INTEGER NOT NULL,
                lookback_days INTEGER NOT NULL,
                notification_mode TEXT NOT NULL,
                digest_hour_local INTEGER NOT NULL,
                enabled INTEGER NOT NULL,
                next_run_at TEXT NOT NULL,
                PRIMARY KEY (athlete_id, source)
            );
            CREATE TABLE automation_sync_runs (
                run_id TEXT PRIMARY KEY,
                athlete_id TEXT NOT NULL,
                source TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                error TEXT
            );
            CREATE TABLE data_change_sets (
                change_set_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL UNIQUE,
                athlete_id TEXT NOT NULL,
                source TEXT NOT NULL,
                record_ids_json TEXT NOT NULL,
                affected_dates_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES automation_sync_runs(run_id)
            );
            CREATE TABLE analysis_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                change_set_id TEXT NOT NULL,
                athlete_id TEXT NOT NULL,
                period TEXT NOT NULL,
                report_json TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (change_set_id, period),
                FOREIGN KEY (change_set_id) REFERENCES data_change_sets(change_set_id)
            );
            CREATE TABLE notification_inbox (
                notification_id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL UNIQUE,
                athlete_id TEXT NOT NULL,
                priority TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                deliver_after TEXT,
                created_at TEXT NOT NULL,
                read_at TEXT,
                delivered_at TEXT,
                delivery_attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                FOREIGN KEY (snapshot_id) REFERENCES analysis_snapshots(snapshot_id)
            );
            INSERT INTO sync_subscriptions VALUES (
                'athlete-001', 'garmin-cn', 30, 3,
                'actionable-digest', 20, 1, '2026-10-26T12:30:00+00:00'
            );
            """
        )
        connection.execute(
            """
            INSERT INTO automation_sync_runs VALUES (
                'run-v1', 'athlete-001', 'garmin-cn', 'schedule',
                'completed', '2026-10-26T12:00:00+00:00', ?, NULL
            )
            """,
            (completed_at,),
        )
        connection.executescript(
            """
            INSERT INTO data_change_sets VALUES (
                'change-v1', 'run-v1', 'athlete-001', 'garmin-cn',
                '["record-v1"]', '["2026-10-26"]', '2026-10-26T12:01:00+00:00'
            );
            INSERT INTO analysis_snapshots VALUES (
                'snapshot-v1', 'change-v1', 'athlete-001', '2026-10-26',
                '{"period":"2026-10-26","signal":true}', 'low',
                '2026-10-26T12:01:00+00:00'
            );
            INSERT INTO notification_inbox VALUES (
                'notification-v1', 'snapshot-v1', 'athlete-001', 'digest',
                'Analysis ready', 'Open the product.', NULL,
                '2026-10-26T12:01:00+00:00', NULL, NULL, 0, NULL
            );
            """
        )
        connection.commit()


def _upgrade_v1_seed_to_running_v3(database_file: Path) -> None:
    with closing(sqlite3.connect(database_file)) as connection:
        connection.executescript(
            """
            CREATE TABLE automation_schema_version (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                version INTEGER NOT NULL
            );
            INSERT INTO automation_schema_version VALUES (1, 3);
            ALTER TABLE automation_sync_runs ADD COLUMN request_key TEXT;
            ALTER TABLE automation_sync_runs ADD COLUMN lease_token TEXT;
            ALTER TABLE automation_sync_runs ADD COLUMN lease_owner TEXT;
            ALTER TABLE automation_sync_runs ADD COLUMN lease_expires_at TEXT;
            ALTER TABLE automation_sync_runs ADD COLUMN updated_at TEXT;
            ALTER TABLE analysis_snapshots ADD COLUMN analysis_kind TEXT NOT NULL DEFAULT 'daily';
            ALTER TABLE analysis_snapshots ADD COLUMN algorithm_version TEXT NOT NULL
                DEFAULT 'wellness-report/1.0';
            ALTER TABLE notification_inbox ADD COLUMN policy_version TEXT NOT NULL
                DEFAULT 'notification-policy/1.0';
            ALTER TABLE notification_inbox ADD COLUMN channel TEXT NOT NULL DEFAULT 'inbox';
            UPDATE automation_sync_runs
            SET status = 'running', completed_at = NULL,
                request_key = 'legacy-v3-slot', lease_token = 'legacy-v3-token',
                lease_owner = 'legacy-worker',
                lease_expires_at = '2026-10-26T12:05:00+00:00',
                updated_at = '2026-10-26T12:01:00+00:00'
            WHERE run_id = 'run-v1';
            """
        )
        connection.commit()


class SQLiteAutomationRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
        self.subscription = SyncSubscription(
            athlete_id="athlete-001",
            source="garmin-cn",
            interval_minutes=30,
            lookback_days=3,
            notification_mode=NotificationMode.ACTIONABLE_DIGEST,
            digest_hour_local=20,
            enabled=True,
        )
        self.result = IngestionResult(
            inserted=1,
            updated=0,
            retired=0,
            duplicates=0,
            raw_files=1,
            changed_record_ids=("record-002", "record-001"),
            affected_dates=(self.now.date(),),
        )

    def seed_notifications(
        self,
        repository: SQLiteAutomationRepository,
        *,
        count: int,
    ) -> list[UserNotification]:
        repository.save_subscription(self.subscription, next_run_at=self.now)
        run = repository.claim_due(
            self.now,
            owner="analysis-worker",
            lease_duration=timedelta(minutes=15),
            limit=1,
        )[0][1]
        assert run.lease_token is not None
        change_set = repository.save_change_set(
            run.run_id,
            self.result,
            created_at=self.now,
            lease_token=run.lease_token,
        )
        notifications = []
        for index in range(count):
            created_at = self.now + timedelta(seconds=index)
            snapshot = repository.save_snapshot(
                change_set,
                {"period": f"2026-10-{26 + index:02d}"},
                "low",
                created_at=created_at,
                analysis_kind="daily",
                run_id=run.run_id,
                lease_token=run.lease_token,
                lease_owner=run.lease_owner,
                lease_checked_at=created_at,
            )
            notification = repository.enqueue_notification(
                snapshot,
                priority="digest",
                deliver_after=None,
                created_at=created_at,
                run_id=run.run_id,
                lease_token=run.lease_token,
                lease_owner=run.lease_owner,
                lease_checked_at=created_at,
            )
            assert notification is not None
            notifications.append(notification)
        repository.complete_sync_run(
            run.run_id,
            completed_at=self.now + timedelta(minutes=1),
            lease_token=run.lease_token,
        )
        return notifications

    def test_persists_automatic_sync_analysis_and_notification_lifecycle(self) -> None:
        later = self.now + timedelta(hours=8)
        report_payload = {
            "period": "2026-10-26",
            "changed_records": 2,
            "summary": "Training data updated.",
        }

        with TemporaryDirectory() as directory:
            database_file = Path(directory) / "db" / "athlete.sqlite3"
            repository = SQLiteAutomationRepository(database_file)
            repository.save_subscription(self.subscription, next_run_at=self.now)

            self.assertEqual(repository.due_subscriptions(self.now), [self.subscription])
            run = repository.start_sync_run(
                self.subscription,
                now=self.now,
                reason="schedule",
                request_key="schedule-slot-001",
            )
            assert run.lease_token is not None
            change_set = repository.save_change_set(
                run.run_id,
                self.result,
                created_at=self.now,
                lease_token=run.lease_token,
            )
            snapshot = repository.save_snapshot(
                change_set,
                report_payload,
                "low",
                created_at=self.now,
                run_id=run.run_id,
                lease_token=run.lease_token,
                lease_owner=run.lease_owner,
                lease_checked_at=self.now,
            )
            notification = repository.enqueue_notification(
                snapshot,
                priority="digest",
                deliver_after=later,
                created_at=self.now,
                run_id=run.run_id,
                lease_token=run.lease_token,
                lease_owner=run.lease_owner,
                lease_checked_at=self.now,
            )

            self.assertEqual(change_set.record_ids, ("record-001", "record-002"))
            self.assertEqual(repository.list_snapshots("athlete-001"), [snapshot])
            self.assertEqual(repository.list_unread("athlete-001"), [notification])
            self.assertEqual(repository.list_pending(self.now), [])
            self.assertEqual(repository.list_pending(later), [notification])
            self.assertEqual(database_file.stat().st_mode & 0o777, 0o600)

    def test_replays_return_existing_change_set_snapshot_and_notification(self) -> None:
        report_payload = {"period": "2026-10-26", "changed_records": 2}

        with TemporaryDirectory() as directory:
            repository = SQLiteAutomationRepository(Path(directory) / "athlete.sqlite3")
            run = repository.start_sync_run(
                self.subscription,
                now=self.now,
                reason="schedule",
                request_key="schedule-slot-001",
            )
            assert run.lease_token is not None

            first_change_set = repository.save_change_set(
                run.run_id,
                self.result,
                created_at=self.now,
                lease_token=run.lease_token,
            )
            replayed_change_set = repository.save_change_set(
                run.run_id,
                self.result,
                created_at=self.now + timedelta(minutes=1),
            )
            first_snapshot = repository.save_snapshot(
                first_change_set,
                report_payload,
                "low",
                created_at=self.now,
                run_id=run.run_id,
                lease_token=run.lease_token,
                lease_owner=run.lease_owner,
                lease_checked_at=self.now,
            )
            replayed_snapshot = repository.save_snapshot(
                replayed_change_set,
                report_payload,
                "low",
                created_at=self.now + timedelta(minutes=1),
            )
            first_notification = repository.enqueue_notification(
                first_snapshot,
                priority="digest",
                deliver_after=None,
                created_at=self.now,
                run_id=run.run_id,
                lease_token=run.lease_token,
                lease_owner=run.lease_owner,
                lease_checked_at=self.now,
            )
            replayed_notification = repository.enqueue_notification(
                replayed_snapshot,
                priority="digest",
                deliver_after=None,
            )

            self.assertEqual(replayed_change_set, first_change_set)
            self.assertEqual(replayed_snapshot, first_snapshot)
            self.assertEqual(replayed_notification, first_notification)
            self.assertEqual(repository.list_snapshots("athlete-001"), [first_snapshot])
            self.assertEqual(repository.list_unread("athlete-001"), [first_notification])

    def test_first_notification_insert_uses_current_off_policy(self) -> None:
        with TemporaryDirectory() as directory:
            repository = SQLiteAutomationRepository(Path(directory) / "athlete.sqlite3")
            repository.save_subscription(self.subscription, next_run_at=self.now)
            run = repository.claim_due(self.now, owner="analysis-worker", limit=1)[0][1]
            assert run.lease_token is not None
            change_set = repository.save_change_set(
                run.run_id,
                self.result,
                created_at=self.now,
                lease_token=run.lease_token,
            )
            snapshot = repository.save_snapshot(
                change_set,
                {"period": "2026-10-26"},
                "low",
                created_at=self.now,
                run_id=run.run_id,
                lease_token=run.lease_token,
                lease_owner=run.lease_owner,
                lease_checked_at=self.now,
            )
            repository.save_subscription(
                replace(self.subscription, notification_mode=NotificationMode.OFF),
                next_run_at=self.now + timedelta(minutes=30),
            )

            notification = repository.enqueue_notification(
                snapshot,
                priority="digest",
                deliver_after=None,
                created_at=self.now,
                run_id=run.run_id,
                lease_token=run.lease_token,
                lease_owner=run.lease_owner,
                lease_checked_at=self.now,
            )

            self.assertIsNone(notification)
            self.assertEqual(repository.list_unread("athlete-001"), [])

    def test_existing_notification_replay_remains_readable_when_policy_is_off(self) -> None:
        with TemporaryDirectory() as directory:
            repository = SQLiteAutomationRepository(Path(directory) / "athlete.sqlite3")
            first = self.seed_notifications(repository, count=1)[0]
            repository.save_subscription(
                replace(self.subscription, notification_mode=NotificationMode.OFF),
                next_run_at=self.now + timedelta(minutes=30),
            )
            snapshot = repository.list_snapshots("athlete-001")[0]

            replay = repository.enqueue_notification(
                snapshot,
                priority="digest",
                deliver_after=None,
                created_at=self.now,
            )

            self.assertEqual(replay, first)
            self.assertEqual(repository.list_unread("athlete-001"), [first])

    def test_analysis_and_notification_identities_support_versions_and_reject_conflicts(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            repository = SQLiteAutomationRepository(Path(directory) / "athlete.sqlite3")
            run = repository.start_sync_run(self.subscription, now=self.now, reason="manual")
            change_set = repository.save_change_set(
                run.run_id,
                self.result,
                created_at=self.now,
            )
            daily_v1 = repository.save_snapshot(
                change_set,
                {"period": "2026-10-26", "score": 82},
                "low",
                analysis_kind="daily",
                algorithm_version="wellness/1.0",
                created_at=self.now,
            )
            weekly_v1 = repository.save_snapshot(
                change_set,
                {"period": "2026-10-26", "score": 82},
                "low",
                analysis_kind="weekly",
                algorithm_version="wellness/1.0",
                created_at=self.now,
            )
            daily_v2 = repository.save_snapshot(
                change_set,
                {"period": "2026-10-26", "score": 83},
                "low",
                analysis_kind="daily",
                algorithm_version="wellness/2.0",
                created_at=self.now,
            )
            replay = repository.save_snapshot(
                change_set,
                {"period": "2026-10-26", "score": 82},
                "low",
                analysis_kind="daily",
                algorithm_version="wellness/1.0",
                created_at=self.now + timedelta(minutes=1),
            )

            self.assertEqual(replay, daily_v1)
            self.assertEqual(
                repository.list_snapshots("athlete-001"),
                [daily_v1, daily_v2, weekly_v1],
            )
            self.assertEqual(len({item.snapshot_id for item in (daily_v1, weekly_v1, daily_v2)}), 3)
            with self.assertRaisesRegex(ValueError, "conflict"):
                repository.save_snapshot(
                    change_set,
                    {"period": "2026-10-26", "score": 99},
                    "high",
                    analysis_kind="daily",
                    algorithm_version="wellness/1.0",
                    created_at=self.now,
                )

            inbox_v1 = repository.enqueue_notification(
                daily_v1,
                priority="digest",
                deliver_after=None,
                policy_version="notification-policy/1.0",
                channel="inbox",
                title="Analysis ready",
                body="Review the daily analysis.",
                created_at=self.now,
            )
            email_v1 = repository.enqueue_notification(
                daily_v1,
                priority="digest",
                deliver_after=None,
                policy_version="notification-policy/1.0",
                channel="email",
                title="Analysis ready",
                body="Review the daily analysis.",
                created_at=self.now,
            )
            inbox_v2 = repository.enqueue_notification(
                daily_v1,
                priority="digest",
                deliver_after=None,
                policy_version="notification-policy/2.0",
                channel="inbox",
                title="Analysis ready",
                body="Review the daily analysis.",
                created_at=self.now,
            )
            notification_replay = repository.enqueue_notification(
                daily_v1,
                priority="digest",
                deliver_after=None,
                policy_version="notification-policy/1.0",
                channel="inbox",
                title="Analysis ready",
                body="Review the daily analysis.",
                created_at=self.now + timedelta(minutes=1),
            )

            self.assertEqual(notification_replay, inbox_v1)
            self.assertEqual(
                len({item.notification_id for item in (inbox_v1, email_v1, inbox_v2)}),
                3,
            )
            with self.assertRaisesRegex(ValueError, "conflict"):
                repository.enqueue_notification(
                    daily_v1,
                    priority="actionable",
                    deliver_after=None,
                    policy_version="notification-policy/1.0",
                    channel="inbox",
                    title="Changed title",
                    body="Changed body",
                    created_at=self.now,
                )

    def test_due_claim_is_atomic_and_stable_across_repository_instances(self) -> None:
        with TemporaryDirectory() as directory:
            database_file = Path(directory) / "athlete.sqlite3"
            first_repository = SQLiteAutomationRepository(database_file)
            second_repository = SQLiteAutomationRepository(database_file)
            first_repository.save_subscription(self.subscription, next_run_at=self.now)

            first_claim = first_repository.claim_due(
                self.now,
                owner="worker-a",
                lease_duration=timedelta(hours=1),
            )
            second_claim = second_repository.claim_due(
                self.now,
                owner="worker-b",
                lease_duration=timedelta(hours=1),
            )

            self.assertEqual(len(first_claim), 1)
            self.assertEqual(second_claim, [])
            claimed_subscription, claimed_run = first_claim[0]
            self.assertEqual(claimed_subscription, self.subscription)
            self.assertIsNotNone(claimed_run.request_key)
            self.assertEqual(claimed_run.lease_owner, "worker-a")
            self.assertEqual(
                claimed_run.lease_expires_at,
                self.now + timedelta(hours=1),
            )
            next_run_at = first_repository.get_next_run_at("athlete-001", "garmin-cn")
            assert next_run_at is not None
            self.assertGreater(next_run_at, self.now)

            reconstructed = SQLiteAutomationRepository(database_file)
            self.assertEqual(reconstructed.claim_due(self.now), [])
            self.assertEqual(
                reconstructed.latest_sync_run("athlete-001", "garmin-cn"),
                claimed_run,
            )
            self.assertEqual(
                reconstructed.list_recoverable_runs(self.now + timedelta(minutes=59)),
                [],
            )

            next_slot = self.now + timedelta(minutes=30)
            reconstructed.schedule_next(self.subscription, next_run_at=next_slot)
            self.assertEqual(
                reconstructed.claim_due(
                    next_slot,
                    owner="worker-c",
                    lease_duration=timedelta(minutes=30),
                ),
                [],
            )

            lease_expired_at = self.now + timedelta(hours=1)
            self.assertEqual(
                reconstructed.list_recoverable_runs(lease_expired_at),
                [claimed_run],
            )
            reclaimed = reconstructed.reclaim_sync_run(
                claimed_run.run_id,
                now=lease_expired_at,
                owner="worker-b",
                lease_duration=timedelta(minutes=30),
            )
            self.assertEqual(reclaimed.run_id, claimed_run.run_id)
            self.assertEqual(reclaimed.request_key, claimed_run.request_key)
            self.assertEqual(reclaimed.lease_owner, "worker-b")
            self.assertEqual(
                reclaimed.lease_expires_at,
                lease_expired_at + timedelta(minutes=30),
            )
            self.assertEqual(reconstructed.list_recoverable_runs(lease_expired_at), [])
            self.assertEqual(
                reconstructed.claim_due(
                    lease_expired_at,
                    owner="worker-c",
                    lease_duration=timedelta(minutes=30),
                ),
                [],
            )
            with closing(sqlite3.connect(database_file)) as connection:
                run_count = connection.execute(
                    "SELECT COUNT(*) FROM automation_sync_runs WHERE request_key = ?",
                    (claimed_run.request_key,),
                ).fetchone()[0]
            self.assertEqual(run_count, 1)

    def test_migrates_v1_schema_atomically_and_reopens_idempotently(self) -> None:
        with TemporaryDirectory() as directory:
            database_file = Path(directory) / "athlete.sqlite3"
            _seed_v1_database(database_file)

            repository = SQLiteAutomationRepository(database_file)
            run = repository.get_sync_run("run-v1")
            assert run is not None
            snapshots = repository.list_snapshots("athlete-001")
            notifications = repository.list_unread("athlete-001")
            change_set = repository.get_change_set_for_run("run-v1")
            assert change_set is not None

            self.assertEqual(run.status, "completed")
            self.assertEqual(run.updated_at, run.completed_at)
            self.assertEqual(snapshots[0].analysis_kind, "wellness")
            self.assertEqual(snapshots[0].algorithm_version, "wellness-report/1.0")
            self.assertEqual(
                notifications[0].policy_version,
                "notification-policy/1.0",
            )
            self.assertEqual(notifications[0].channel, "inbox")
            self.assertEqual(
                change_set.projection_record_ids,
                ("__legacy_projection_unavailable__",),
            )
            self.assertEqual(
                notifications[0].next_attempt_at,
                notifications[0].created_at,
            )

            reopened = SQLiteAutomationRepository(database_file)
            self.assertEqual(reopened.get_sync_run("run-v1"), run)
            self.assertEqual(reopened.list_snapshots("athlete-001"), snapshots)
            with closing(sqlite3.connect(database_file)) as connection:
                version = connection.execute(
                    "SELECT version FROM automation_schema_version WHERE singleton = 1"
                ).fetchone()[0]
                table_names = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                run_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(automation_sync_runs)"
                    ).fetchall()
                }
                run_ddl = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'automation_sync_runs'"
                ).fetchone()[0]
                foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
                backup_run_count = connection.execute(
                    "SELECT COUNT(*) FROM automation_sync_runs_automation_v1_backup"
                ).fetchone()[0]
                with self.assertRaisesRegex(sqlite3.IntegrityError, "read-only backup"):
                    connection.execute("DELETE FROM automation_sync_runs_automation_v1_backup")
                connection.rollback()

            self.assertEqual(version, 4)
            self.assertTrue(
                {
                    "sync_subscriptions_automation_v1_backup",
                    "automation_sync_runs_automation_v1_backup",
                    "data_change_sets_automation_v1_backup",
                    "analysis_snapshots_automation_v1_backup",
                    "notification_inbox_automation_v1_backup",
                }
                <= table_names
            )
            self.assertTrue(
                {
                    "request_key",
                    "lease_token",
                    "lease_owner",
                    "lease_expires_at",
                    "updated_at",
                }
                <= run_columns
            )
            self.assertIn("completed_at >= started_at", run_ddl)
            self.assertEqual(foreign_key_errors, [])
            self.assertEqual(backup_run_count, 1)

    def test_legacy_migration_reconstructs_full_current_projection(self) -> None:
        with TemporaryDirectory() as directory:
            database_file = Path(directory) / "athlete.sqlite3"
            records = SQLiteRecordStore(database_file)
            current = tuple(
                CanonicalRecord.create(
                    athlete_id="athlete-001",
                    kind="activity",
                    occurred_at=self.now - timedelta(days=days_ago),
                    data={"training_load": load, "duration_min": 30},
                )
                for days_ago, load in ((0, 10), (1, 20))
            )
            for record in current:
                records.save(record)
            _seed_v1_database(database_file)
            with closing(sqlite3.connect(database_file)) as connection:
                connection.execute(
                    "UPDATE data_change_sets SET record_ids_json = ? WHERE change_set_id = ?",
                    (f'["{current[0].record_id}"]', "change-v1"),
                )
                connection.commit()

            repository = SQLiteAutomationRepository(database_file)
            change_set = repository.get_change_set_for_run("run-v1")
            assert change_set is not None
            recovered_report = ReportService(
                "athlete-001",
                records,
                projection_record_ids=change_set.projection_record_ids,
            ).weekly(self.now.date())

            self.assertEqual(
                change_set.projection_record_ids,
                tuple(sorted(record.record_id for record in current)),
            )
            self.assertEqual(recovered_report.analytics.metrics["sessions_7d"], 2)

    def test_running_v3_migration_regenerates_effects_from_one_current_projection(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database_file = root / "athlete.sqlite3"
            raw_root = root / "raw"
            records = SQLiteRecordStore(database_file)
            current: list[CanonicalRecord] = []
            evidence_hashes: set[str] = set()
            for index, load in enumerate((10, 20), start=1):
                payload = {"training_load": load, "duration_min": 30}
                payload_bytes = json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                digest = hashlib.sha256(payload_bytes).hexdigest()
                evidence_hashes.add(digest)
                raw_file = raw_root / "athlete-001" / "garmin-cn" / digest[:2] / f"{digest}.json"
                raw_file.parent.mkdir(parents=True, exist_ok=True)
                raw_file.write_bytes(payload_bytes)
                record = CanonicalRecord.create(
                    athlete_id="athlete-001",
                    kind="activity",
                    occurred_at=datetime(2026, 10, 26, 8 + index, 0, tzinfo=UTC),
                    data=payload,
                    evidence=(
                        EvidenceRef(
                            "garmin-cn",
                            f"activity-{index}",
                            datetime(2026, 10, 26, 12, 11, tzinfo=UTC),
                            digest,
                        ),
                    ),
                    created_at=datetime(2026, 10, 26, 12, 11, tzinfo=UTC),
                )
                records.save(record)
                current.append(record)
            _seed_v1_database(database_file)
            with closing(sqlite3.connect(database_file)) as connection:
                connection.execute(
                    "UPDATE data_change_sets SET record_ids_json = ? WHERE change_set_id = ?",
                    (json.dumps([current[0].record_id]), "change-v1"),
                )
                connection.commit()
            _upgrade_v1_seed_to_running_v3(database_file)

            repository = SQLiteAutomationRepository(database_file)
            migrated = repository.get_change_set_for_run("run-v1")
            assert migrated is not None
            self.assertNotEqual(migrated.change_set_id, "change-v1")
            self.assertEqual(
                migrated.projection_record_ids,
                tuple(sorted(record.record_id for record in current)),
            )
            self.assertEqual(repository.list_snapshots_for_change_set(migrated.change_set_id), [])
            self.assertEqual(
                repository.list_notifications_for_change_set(migrated.change_set_id),
                [],
            )
            with closing(sqlite3.connect(database_file)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM analysis_snapshots_automation_v3_backup"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM notification_inbox_automation_v3_backup"
                    ).fetchone()[0],
                    1,
                )

            now = datetime(2026, 10, 26, 12, 20, tzinfo=UTC)
            profiles = SQLiteAthleteProfileRepository(database_file)
            profiles.save(
                AthleteProfile(
                    athlete_id="athlete-001",
                    display_name="Migration Athlete",
                    timezone_name="Asia/Shanghai",
                    unit_system="metric",
                    created_at=now,
                )
            )
            loop = AutomaticTrainingLoop(
                automation_repository=repository,
                record_store=records,
                profile_repository=profiles,
                connectors={},
                raw_root=raw_root,
                clock=lambda: now,
                owner="migration-recovery-worker",
                lease_duration=timedelta(minutes=5),
            )

            result = loop.run_once(limit=1)

            recovered = repository.get_sync_run("run-v1")
            assert recovered is not None
            snapshots = repository.list_snapshots_for_change_set(migrated.change_set_id)
            notifications = repository.list_notifications_for_change_set(migrated.change_set_id)
            self.assertEqual((result.recovered, result.completed, result.failed), (1, 1, 0))
            self.assertEqual(recovered.status, "completed")
            self.assertEqual(
                {snapshot.analysis_kind for snapshot in snapshots},
                {"daily", "weekly"},
            )
            self.assertEqual(len(snapshots), 2)
            self.assertTrue(
                all(
                    set(snapshot.report["evidence_ids"]) == evidence_hashes
                    for snapshot in snapshots
                )
            )
            self.assertEqual(len(notifications), 1)

    def test_invalid_v1_run_aborts_migration_without_partial_schema(self) -> None:
        with TemporaryDirectory() as directory:
            database_file = Path(directory) / "athlete.sqlite3"
            _seed_v1_database(database_file, invalid_run=True)

            with self.assertRaisesRegex(ValueError, "invalid legacy sync run"):
                SQLiteAutomationRepository(database_file)

            with closing(sqlite3.connect(database_file)) as connection:
                table_names = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                snapshot_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(analysis_snapshots)"
                    ).fetchall()
                }
                completed_at = connection.execute(
                    "SELECT completed_at FROM automation_sync_runs WHERE run_id = 'run-v1'"
                ).fetchone()[0]

            self.assertNotIn("automation_schema_version", table_names)
            self.assertFalse(any(name.endswith("_backup") for name in table_names))
            self.assertNotIn("analysis_kind", snapshot_columns)
            self.assertIsNone(completed_at)

    def test_concurrent_constructors_serialize_v1_migration(self) -> None:
        with TemporaryDirectory() as directory:
            database_file = Path(directory) / "athlete.sqlite3"
            _seed_v1_database(database_file)
            barrier = Barrier(2)

            def construct_and_read() -> str:
                barrier.wait()
                repository = SQLiteAutomationRepository(database_file)
                run = repository.get_sync_run("run-v1")
                assert run is not None
                return run.run_id

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: construct_and_read(), range(2)))

            self.assertEqual(results, ["run-v1", "run-v1"])
            with closing(sqlite3.connect(database_file)) as connection:
                version = connection.execute(
                    "SELECT version FROM automation_schema_version WHERE singleton = 1"
                ).fetchone()[0]
                foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
                live_tables = connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
                    "AND name IN ('automation_sync_runs', 'analysis_snapshots', "
                    "'notification_inbox')"
                ).fetchone()[0]
                backup_tables = connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
                    "AND name LIKE '%_automation_v1_backup'"
                ).fetchone()[0]
                backup_run_count = connection.execute(
                    "SELECT COUNT(*) FROM automation_sync_runs_automation_v1_backup"
                ).fetchone()[0]

            self.assertEqual(version, 4)
            self.assertEqual(foreign_key_errors, [])
            self.assertEqual(live_tables, 3)
            self.assertEqual(backup_tables, 5)
            self.assertEqual(backup_run_count, 1)

    def test_direct_scheduled_start_requires_a_stable_request_key(self) -> None:
        with TemporaryDirectory() as directory:
            repository = SQLiteAutomationRepository(Path(directory) / "athlete.sqlite3")
            with self.assertRaisesRegex(ValueError, "claim_due"):
                repository.start_sync_run(
                    self.subscription,
                    now=self.now,
                    reason="schedule",
                )

            first = repository.start_sync_run(
                self.subscription,
                now=self.now,
                reason="schedule",
                request_key="schedule-slot-001",
            )
            replayed = repository.start_sync_run(
                self.subscription,
                now=self.now + timedelta(seconds=1),
                reason="schedule",
                request_key="schedule-slot-001",
            )

            self.assertEqual(replayed, first)

    def test_reclaim_rotates_lease_token_and_fences_the_stale_worker(self) -> None:
        with TemporaryDirectory() as directory:
            repository = SQLiteAutomationRepository(Path(directory) / "athlete.sqlite3")
            repository.save_subscription(self.subscription, next_run_at=self.now)
            claimed = repository.claim_due(
                self.now,
                owner="worker-old",
                lease_duration=timedelta(minutes=5),
            )[0][1]
            assert claimed.lease_token is not None

            reclaimed_at = self.now + timedelta(minutes=5)
            reclaimed = repository.reclaim_sync_run(
                claimed.run_id,
                now=reclaimed_at,
                owner="worker-new",
                lease_duration=timedelta(minutes=10),
            )
            assert reclaimed.lease_token is not None
            self.assertNotEqual(reclaimed.lease_token, claimed.lease_token)

            terminal_at = reclaimed_at + timedelta(minutes=1)
            with self.assertRaisesRegex(ValueError, "lease token"):
                repository.complete_sync_run(
                    claimed.run_id,
                    completed_at=terminal_at,
                    lease_token=claimed.lease_token,
                )
            with self.assertRaisesRegex(ValueError, "lease token"):
                repository.fail_sync_run(
                    claimed.run_id,
                    error="stale worker failure",
                    completed_at=terminal_at,
                    lease_token=claimed.lease_token,
                )
            with self.assertRaisesRegex(ValueError, "lease token"):
                repository.complete_sync_run(
                    claimed.run_id,
                    completed_at=terminal_at,
                )
            self.assertEqual(repository.get_sync_run(claimed.run_id), reclaimed)

            completed = repository.complete_sync_run(
                claimed.run_id,
                completed_at=terminal_at,
                lease_token=reclaimed.lease_token,
            )
            self.assertEqual(completed.status, "completed")

    def test_renewal_requires_current_owner_and_token_and_enforces_chronology(self) -> None:
        with TemporaryDirectory() as directory:
            repository = SQLiteAutomationRepository(Path(directory) / "athlete.sqlite3")
            repository.save_subscription(self.subscription, next_run_at=self.now)
            claimed = repository.claim_due(
                self.now,
                owner="worker-a",
                lease_duration=timedelta(minutes=10),
            )[0][1]
            assert claimed.lease_token is not None

            renewed_at = self.now + timedelta(minutes=5)
            with self.assertRaisesRegex(ValueError, "lease token"):
                repository.renew_lease(
                    claimed.run_id,
                    "wrong-token",
                    owner="worker-a",
                    now=renewed_at,
                    lease_duration=timedelta(minutes=30),
                )
            with self.assertRaisesRegex(ValueError, "lease owner"):
                repository.renew_lease(
                    claimed.run_id,
                    claimed.lease_token,
                    owner="worker-b",
                    now=renewed_at,
                    lease_duration=timedelta(minutes=30),
                )
            self.assertEqual(repository.get_sync_run(claimed.run_id), claimed)

            renewed = repository.renew_lease(
                claimed.run_id,
                claimed.lease_token,
                owner="worker-a",
                now=renewed_at,
                lease_duration=timedelta(minutes=30),
            )
            self.assertEqual(renewed.lease_token, claimed.lease_token)
            self.assertEqual(renewed.updated_at, renewed_at)
            self.assertEqual(
                renewed.lease_expires_at,
                renewed_at + timedelta(minutes=30),
            )

            with self.assertRaisesRegex(ValueError, "updated_at"):
                repository.complete_sync_run(
                    claimed.run_id,
                    completed_at=renewed_at - timedelta(seconds=1),
                    lease_token=renewed.lease_token,
                )
            self.assertEqual(repository.get_sync_run(claimed.run_id), renewed)

    def test_change_set_first_write_is_fenced_by_the_current_lease_token(self) -> None:
        with TemporaryDirectory() as directory:
            repository = SQLiteAutomationRepository(Path(directory) / "athlete.sqlite3")
            repository.save_subscription(self.subscription, next_run_at=self.now)
            claimed = repository.claim_due(
                self.now,
                owner="worker-old",
                lease_duration=timedelta(minutes=5),
            )[0][1]
            assert claimed.lease_token is not None
            reclaimed = repository.reclaim_sync_run(
                claimed.run_id,
                now=self.now + timedelta(minutes=5),
                owner="worker-new",
                lease_duration=timedelta(minutes=10),
            )
            assert reclaimed.lease_token is not None

            with self.assertRaisesRegex(ValueError, "lease token"):
                repository.save_change_set(
                    claimed.run_id,
                    self.result,
                    created_at=self.now + timedelta(minutes=6),
                )
            with self.assertRaisesRegex(ValueError, "lease token"):
                repository.save_change_set(
                    claimed.run_id,
                    self.result,
                    created_at=self.now + timedelta(minutes=6),
                    lease_token=claimed.lease_token,
                )
            change_set = repository.save_change_set(
                claimed.run_id,
                self.result,
                created_at=self.now + timedelta(minutes=6),
                lease_token=reclaimed.lease_token,
            )
            self.assertEqual(change_set.run_id, claimed.run_id)

    def test_change_set_cannot_be_first_written_after_new_owner_completes(self) -> None:
        with TemporaryDirectory() as directory:
            repository = SQLiteAutomationRepository(Path(directory) / "athlete.sqlite3")
            repository.save_subscription(self.subscription, next_run_at=self.now)
            claimed = repository.claim_due(
                self.now,
                owner="worker-old",
                lease_duration=timedelta(minutes=5),
            )[0][1]
            assert claimed.lease_token is not None
            reclaimed_at = self.now + timedelta(minutes=5)
            reclaimed = repository.reclaim_sync_run(
                claimed.run_id,
                now=reclaimed_at,
                owner="worker-new",
                lease_duration=timedelta(minutes=10),
            )
            assert reclaimed.lease_token is not None
            repository.complete_sync_run(
                claimed.run_id,
                completed_at=reclaimed_at + timedelta(minutes=1),
                lease_token=reclaimed.lease_token,
            )

            with self.assertRaisesRegex(ValueError, "running"):
                repository.save_change_set(
                    claimed.run_id,
                    self.result,
                    created_at=reclaimed_at + timedelta(minutes=2),
                    lease_token=claimed.lease_token,
                )

    def test_effect_writes_are_lease_fenced_and_exact_terminal_replays_are_allowed(self) -> None:
        with TemporaryDirectory() as directory:
            repository = SQLiteAutomationRepository(Path(directory) / "athlete.sqlite3")
            repository.save_subscription(self.subscription, next_run_at=self.now)
            claimed = repository.claim_due(
                self.now,
                owner="worker-old",
                lease_duration=timedelta(minutes=5),
            )[0][1]
            assert claimed.lease_token is not None
            change_set = repository.save_change_set(
                claimed.run_id,
                self.result,
                created_at=self.now,
                lease_token=claimed.lease_token,
            )
            reclaimed_at = self.now + timedelta(minutes=5)
            reclaimed = repository.reclaim_sync_run(
                claimed.run_id,
                now=reclaimed_at,
                owner="worker-new",
                lease_duration=timedelta(minutes=10),
            )
            assert reclaimed.lease_token is not None
            effect_at = reclaimed_at + timedelta(minutes=1)
            report = {"period": "2026-10-26", "score": 80}

            with self.assertRaisesRegex(ValueError, "lease"):
                repository.save_snapshot(
                    change_set,
                    report,
                    "low",
                    created_at=effect_at,
                    run_id=claimed.run_id,
                    lease_token=claimed.lease_token,
                    lease_owner=claimed.lease_owner,
                    lease_checked_at=effect_at,
                )
            self.assertEqual(repository.list_snapshots("athlete-001"), [])
            snapshot = repository.save_snapshot(
                change_set,
                report,
                "low",
                created_at=effect_at,
                run_id=reclaimed.run_id,
                lease_token=reclaimed.lease_token,
                lease_owner=reclaimed.lease_owner,
                lease_checked_at=effect_at,
            )
            with self.assertRaisesRegex(ValueError, "lease"):
                repository.enqueue_notification(
                    snapshot,
                    priority="digest",
                    deliver_after=None,
                    created_at=effect_at,
                    run_id=claimed.run_id,
                    lease_token=claimed.lease_token,
                    lease_owner=claimed.lease_owner,
                    lease_checked_at=effect_at,
                )
            self.assertEqual(repository.list_unread("athlete-001"), [])
            notification = repository.enqueue_notification(
                snapshot,
                priority="digest",
                deliver_after=None,
                created_at=effect_at,
                run_id=reclaimed.run_id,
                lease_token=reclaimed.lease_token,
                lease_owner=reclaimed.lease_owner,
                lease_checked_at=effect_at,
            )
            self.assertEqual(repository.get_sync_run(claimed.run_id), reclaimed)
            repository.complete_sync_run(
                reclaimed.run_id,
                completed_at=effect_at + timedelta(minutes=1),
                lease_token=reclaimed.lease_token,
            )

            self.assertEqual(
                repository.save_snapshot(
                    change_set,
                    report,
                    "low",
                    created_at=effect_at + timedelta(minutes=2),
                ),
                snapshot,
            )
            self.assertEqual(
                repository.enqueue_notification(
                    snapshot,
                    priority="digest",
                    deliver_after=None,
                    created_at=effect_at + timedelta(minutes=2),
                ),
                notification,
            )
            with self.assertRaisesRegex(ValueError, "conflict"):
                repository.save_snapshot(
                    change_set,
                    {"period": "2026-10-26", "score": 99},
                    "high",
                    created_at=effect_at + timedelta(minutes=2),
                )

    def test_exact_change_set_replay_survives_completion_but_divergence_conflicts(self) -> None:
        divergent_result = IngestionResult(
            inserted=1,
            updated=0,
            retired=0,
            duplicates=0,
            raw_files=1,
            changed_record_ids=("record-divergent",),
            affected_dates=(self.now.date(),),
        )
        with TemporaryDirectory() as directory:
            repository = SQLiteAutomationRepository(Path(directory) / "athlete.sqlite3")
            repository.save_subscription(self.subscription, next_run_at=self.now)
            run = repository.claim_due(
                self.now,
                owner="worker-a",
                lease_duration=timedelta(minutes=10),
            )[0][1]
            assert run.lease_token is not None
            first = repository.save_change_set(
                run.run_id,
                self.result,
                created_at=self.now,
                lease_token=run.lease_token,
            )
            repository.complete_sync_run(
                run.run_id,
                completed_at=self.now + timedelta(minutes=1),
                lease_token=run.lease_token,
            )

            replay = repository.save_change_set(
                run.run_id,
                self.result,
                created_at=self.now + timedelta(minutes=2),
            )
            self.assertEqual(replay, first)
            with self.assertRaisesRegex(ValueError, "conflict"):
                repository.save_change_set(
                    run.run_id,
                    divergent_result,
                    created_at=self.now + timedelta(minutes=2),
                )

    def test_terminal_run_updates_are_idempotent_and_cannot_be_overwritten(self) -> None:
        completed_at = self.now + timedelta(minutes=1)
        failed_at = self.now + timedelta(minutes=2)

        with TemporaryDirectory() as directory:
            repository = SQLiteAutomationRepository(Path(directory) / "athlete.sqlite3")
            completed_run = repository.start_sync_run(
                self.subscription,
                now=self.now,
                reason="manual",
            )
            first_completed = repository.complete_sync_run(
                completed_run.run_id,
                completed_at=completed_at,
            )
            replayed_completed = repository.complete_sync_run(
                completed_run.run_id,
                completed_at=completed_at,
            )

            self.assertEqual(replayed_completed, first_completed)
            with self.assertRaisesRegex(ValueError, "conflict"):
                repository.fail_sync_run(
                    completed_run.run_id,
                    error="late failure",
                    completed_at=failed_at,
                )
            self.assertEqual(repository.get_sync_run(completed_run.run_id), first_completed)

            failed_run = repository.start_sync_run(
                self.subscription,
                now=self.now,
                reason="manual",
            )
            first_failed = repository.fail_sync_run(
                failed_run.run_id,
                error="network unavailable",
                completed_at=failed_at,
            )
            replayed_failed = repository.fail_sync_run(
                failed_run.run_id,
                error="network unavailable",
                completed_at=failed_at,
            )

            self.assertEqual(replayed_failed, first_failed)
            with self.assertRaisesRegex(ValueError, "conflict"):
                repository.complete_sync_run(
                    failed_run.run_id,
                    completed_at=failed_at,
                )
            self.assertEqual(repository.get_sync_run(failed_run.run_id), first_failed)

            invalid_run = repository.start_sync_run(
                self.subscription,
                now=self.now,
                reason="manual",
            )
            with self.assertRaisesRegex(ValueError, "completed_at"):
                repository.complete_sync_run(
                    invalid_run.run_id,
                    completed_at=self.now - timedelta(seconds=1),
                )
            self.assertEqual(repository.get_sync_run(invalid_run.run_id), invalid_run)

    def test_read_and_delivery_state_are_durable(self) -> None:
        with TemporaryDirectory() as directory:
            database_file = Path(directory) / "athlete.sqlite3"
            repository = SQLiteAutomationRepository(database_file)
            run = repository.start_sync_run(
                self.subscription,
                now=self.now,
                reason="schedule",
                request_key="schedule-slot-001",
            )
            assert run.lease_token is not None
            change_set = repository.save_change_set(
                run.run_id,
                self.result,
                created_at=self.now,
                lease_token=run.lease_token,
            )
            snapshot = repository.save_snapshot(
                change_set,
                {"period": "2026-10-26"},
                "moderate",
                created_at=self.now,
                run_id=run.run_id,
                lease_token=run.lease_token,
                lease_owner=run.lease_owner,
                lease_checked_at=self.now,
            )
            notification = repository.enqueue_notification(
                snapshot,
                priority="actionable",
                deliver_after=None,
                created_at=self.now,
                run_id=run.run_id,
                lease_token=run.lease_token,
                lease_owner=run.lease_owner,
                lease_checked_at=self.now,
            )
            failed = repository.mark_delivery_failed(
                notification.notification_id,
                "host unavailable",
            )
            delivered_at = self.now + timedelta(minutes=2)
            delivered = repository.mark_delivered(
                notification.notification_id,
                delivered_at=delivered_at,
            )
            read_at = self.now + timedelta(minutes=3)
            read = repository.mark_read(notification.notification_id, read_at=read_at)

            self.assertEqual(failed.delivery_attempts, 1)
            self.assertEqual(failed.last_error, "host unavailable")
            self.assertEqual(delivered.delivery_attempts, 2)
            self.assertEqual(delivered.delivered_at, delivered_at)
            self.assertIsNone(delivered.last_error)
            self.assertEqual(read.read_at, read_at)
            self.assertEqual(repository.list_pending(delivered_at), [])
            self.assertEqual(repository.list_unread("athlete-001"), [])

            reopened = SQLiteAutomationRepository(database_file)
            self.assertEqual(reopened.get_notification(notification.notification_id), read)

    def test_pending_delivery_claims_are_atomic_limited_and_ordered(self) -> None:
        with TemporaryDirectory() as directory:
            database_file = Path(directory) / "athlete.sqlite3"
            first_repository = SQLiteAutomationRepository(database_file)
            second_repository = SQLiteAutomationRepository(database_file)
            expected = self.seed_notifications(first_repository, count=3)

            first_claim = first_repository.claim_pending(
                self.now + timedelta(minutes=2),
                owner="delivery-a",
                lease_duration=timedelta(minutes=5),
                limit=2,
            )
            second_claim = second_repository.claim_pending(
                self.now + timedelta(minutes=2),
                owner="delivery-b",
                lease_duration=timedelta(minutes=5),
                limit=2,
            )

            self.assertEqual(
                [item.notification_id for item in first_claim],
                [item.notification_id for item in expected[:2]],
            )
            self.assertEqual(
                [item.notification_id for item in second_claim],
                [expected[2].notification_id],
            )
            self.assertTrue(all(item.delivery_lease_token for item in first_claim))
            self.assertTrue(all(item.delivery_lease_owner == "delivery-a" for item in first_claim))

    def test_pending_claim_uses_authoritative_clock_after_database_lock(self) -> None:
        with TemporaryDirectory() as directory:
            database_file = Path(directory) / "athlete.sqlite3"
            repository = SQLiteAutomationRepository(database_file)
            self.seed_notifications(repository, count=1)
            authoritative = [self.now]
            started = Event()

            def claim_after_signal():
                started.set()
                return repository.claim_pending(
                    self.now,
                    owner="delivery-worker",
                    lease_duration=timedelta(minutes=1),
                    limit=1,
                    lease_clock=lambda: authoritative[0],
                )

            with closing(sqlite3.connect(database_file)) as blocker:
                blocker.execute("BEGIN IMMEDIATE")
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(claim_after_signal)
                    self.assertTrue(started.wait(1))
                    authoritative[0] = self.now + timedelta(minutes=10)
                    blocker.commit()
                    claimed = future.result(timeout=2)[0]

            self.assertEqual(
                claimed.delivery_lease_expires_at,
                authoritative[0] + timedelta(minutes=1),
            )

    def test_pending_claim_rolls_back_if_authoritative_clock_expires_before_commit(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            repository = SQLiteAutomationRepository(Path(directory) / "athlete.sqlite3")
            self.seed_notifications(repository, count=1)
            readings = iter((self.now, self.now + timedelta(minutes=2)))

            with self.assertRaisesRegex(ValueError, "expired"):
                repository.claim_pending(
                    self.now,
                    owner="delivery-worker",
                    lease_duration=timedelta(minutes=1),
                    limit=1,
                    lease_clock=lambda: next(readings),
                )

            pending = repository.list_pending(self.now)
            self.assertEqual(len(pending), 1)
            self.assertIsNone(pending[0].delivery_lease_token)

    def test_expired_delivery_claim_is_reclaimed_and_stale_receipt_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            database_file = Path(directory) / "athlete.sqlite3"
            repository = SQLiteAutomationRepository(database_file)
            notification = self.seed_notifications(repository, count=1)[0]
            claimed = repository.claim_pending(
                self.now + timedelta(minutes=2),
                owner="delivery-a",
                lease_duration=timedelta(minutes=1),
                limit=1,
            )[0]
            reclaimed_at = self.now + timedelta(minutes=4)
            reclaimed = repository.claim_pending(
                reclaimed_at,
                owner="delivery-b",
                lease_duration=timedelta(minutes=1),
                limit=1,
            )[0]

            with self.assertRaisesRegex(ValueError, "delivery lease"):
                repository.mark_delivered(
                    notification.notification_id,
                    delivered_at=reclaimed_at,
                    lease_token=claimed.delivery_lease_token,
                    owner="delivery-a",
                )

            delivered = repository.mark_delivered(
                notification.notification_id,
                delivered_at=reclaimed_at,
                lease_token=reclaimed.delivery_lease_token,
                owner="delivery-b",
            )
            self.assertIsNotNone(delivered.delivered_at)
            self.assertIsNone(delivered.delivery_lease_token)

    def test_delivery_backoff_is_exponential_and_capped(self) -> None:
        self.assertEqual(
            SQLiteAutomationRepository._delivery_backoff(1),
            timedelta(minutes=1),
        )
        self.assertEqual(
            SQLiteAutomationRepository._delivery_backoff(2),
            timedelta(minutes=2),
        )
        self.assertEqual(
            SQLiteAutomationRepository._delivery_backoff(20),
            timedelta(hours=1),
        )

    def test_change_set_projection_is_idempotent_and_part_of_conflict_detection(self) -> None:
        with TemporaryDirectory() as directory:
            repository = SQLiteAutomationRepository(Path(directory) / "athlete.sqlite3")
            run = repository.start_sync_run(
                self.subscription,
                now=self.now,
                reason="schedule",
                request_key="projection-slot",
            )
            assert run.lease_token is not None

            first = repository.save_change_set(
                run.run_id,
                self.result,
                created_at=self.now,
                lease_token=run.lease_token,
                projection_record_ids=("projection-b", "projection-a"),
            )
            replay = repository.save_change_set(
                run.run_id,
                self.result,
                created_at=self.now,
                lease_token=run.lease_token,
                projection_record_ids=("projection-a", "projection-b"),
            )

            self.assertEqual(first.projection_record_ids, ("projection-a", "projection-b"))
            self.assertEqual(replay, first)
            with self.assertRaisesRegex(ValueError, "idempotency conflict"):
                repository.save_change_set(
                    run.run_id,
                    self.result,
                    created_at=self.now,
                    lease_token=run.lease_token,
                    projection_record_ids=("different",),
                )

    def test_failed_run_and_next_run_schedule_survive_reconstruction(self) -> None:
        next_run_at = self.now + timedelta(minutes=30)
        failed_at = self.now + timedelta(minutes=1)

        with TemporaryDirectory() as directory:
            database_file = Path(directory) / "athlete.sqlite3"
            repository = SQLiteAutomationRepository(database_file)
            repository.save_subscription(self.subscription, next_run_at=self.now)
            run = repository.start_sync_run(
                self.subscription,
                now=self.now,
                reason="schedule",
                request_key="schedule-slot-001",
            )
            assert run.lease_token is not None
            failed = repository.fail_sync_run(
                run.run_id,
                error="authentication expired",
                completed_at=failed_at,
                lease_token=run.lease_token,
            )
            repository.schedule_next(self.subscription, next_run_at=next_run_at)

            reopened = SQLiteAutomationRepository(database_file)

            self.assertEqual(reopened.get_sync_run(run.run_id), failed)
            self.assertEqual(reopened.latest_sync_run("athlete-001", "garmin-cn"), failed)
            self.assertEqual(reopened.due_subscriptions(self.now), [])
            self.assertEqual(reopened.due_subscriptions(next_run_at), [self.subscription])
            self.assertEqual(
                reopened.get_next_run_at("athlete-001", "garmin-cn"),
                next_run_at,
            )

    def test_completed_run_is_persisted(self) -> None:
        completed_at = self.now + timedelta(minutes=1)

        with TemporaryDirectory() as directory:
            repository = SQLiteAutomationRepository(Path(directory) / "athlete.sqlite3")
            run = repository.start_sync_run(self.subscription, now=self.now, reason="manual")

            completed = repository.complete_sync_run(
                run.run_id,
                completed_at=completed_at,
            )

            self.assertEqual(completed.status, "completed")
            self.assertEqual(completed.completed_at, completed_at)
            self.assertIsNone(completed.error)
            self.assertEqual(repository.get_sync_run(run.run_id), completed)

    def test_disabled_subscription_is_not_due(self) -> None:
        disabled = SyncSubscription(
            athlete_id="athlete-001",
            source="garmin-cn",
            interval_minutes=30,
            lookback_days=3,
            notification_mode=NotificationMode.OFF,
            digest_hour_local=20,
            enabled=False,
        )

        with TemporaryDirectory() as directory:
            repository = SQLiteAutomationRepository(Path(directory) / "athlete.sqlite3")
            repository.save_subscription(disabled, next_run_at=self.now)

            self.assertEqual(repository.due_subscriptions(self.now), [])
            self.assertEqual(repository.list_subscriptions("athlete-001"), [disabled])

    def test_models_reject_invalid_values_and_naive_datetimes(self) -> None:
        invalid_subscription_values = (
            {"athlete_id": "../escaped"},
            {"athlete_id": "/absolute"},
            {"athlete_id": "nested/athlete"},
            {"athlete_id": r"nested\athlete"},
            {"source": "."},
            {"source": ".."},
            {"source": "../escaped"},
            {"source": "/absolute"},
            {"source": "nested/source"},
            {"source": r"nested\source"},
            {"interval_minutes": 0},
            {"interval_minutes": "30"},
            {"lookback_days": 0},
            {"lookback_days": True},
            {"digest_hour_local": 24},
            {"digest_hour_local": 20.0},
            {"notification_mode": "sometimes"},
        )
        defaults: dict[str, object] = {
            "athlete_id": "athlete-001",
            "source": "garmin-cn",
            "interval_minutes": 30,
            "lookback_days": 3,
            "notification_mode": NotificationMode.ALL,
            "digest_hour_local": 20,
            "enabled": True,
        }
        for overrides in invalid_subscription_values:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                SyncSubscription(**(defaults | overrides))  # type: ignore[arg-type]

        with TemporaryDirectory() as directory:
            repository = SQLiteAutomationRepository(Path(directory) / "athlete.sqlite3")
            with self.assertRaisesRegex(ValueError, "timezone-aware"):
                repository.save_subscription(
                    self.subscription,
                    next_run_at=datetime(2026, 10, 26, 12, 0),
                )

    def test_ingestion_service_imports_before_storage_repository_in_fresh_process(self) -> None:
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from athlete_growth.application.ingest.service "
                    "import IngestionService; "
                    "from athlete_growth.adapters.storage "
                    "import SQLiteAutomationRepository; "
                    "print(IngestionService.__name__, SQLiteAutomationRepository.__name__)"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout.strip(), "IngestionService SQLiteAutomationRepository")

    def test_analysis_snapshot_report_is_deeply_immutable(self) -> None:
        payload: dict[str, JsonValue] = {
            "period": "2026-10-26",
            "details": {
                "score": 82,
                "signals": [{"name": "load", "value": 12}],
            },
        }
        snapshot = AnalysisSnapshot(
            snapshot_id="snapshot-001",
            change_set_id="change-set-001",
            athlete_id="athlete-001",
            period="2026-10-26",
            report=payload,
            risk_level="low",
            created_at=self.now,
        )
        details = cast(dict[str, JsonValue], snapshot.report["details"])
        signals = cast(list[JsonValue], details["signals"])
        first_signal = cast(dict[str, JsonValue], signals[0])

        with self.assertRaises(TypeError):
            snapshot.report["period"] = "changed"  # type: ignore[index]
        with self.assertRaises(TypeError):
            details["score"] = 0
        with self.assertRaises(TypeError):
            signals.append("changed")
        with self.assertRaises(TypeError):
            first_signal["value"] = 0
        with self.assertRaises(TypeError):
            dict.__setitem__(snapshot.report, "period", "base-mutator-bypass")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            dict.__setitem__(details, "score", 0)
        with self.assertRaises(TypeError):
            list.__setitem__(signals, 0, "base-mutator-bypass")
        report_backing = cast(Any, snapshot.report)._values
        details_backing = cast(Any, details)._values
        signals_backing = cast(Any, signals)._values
        with self.assertRaises(TypeError):
            report_backing[0] = "backing-bypass"
        with self.assertRaises(TypeError):
            details_backing[0] = "backing-bypass"
        with self.assertRaises(TypeError):
            signals_backing[0] = "backing-bypass"

        cast(dict[str, JsonValue], payload["details"])["score"] = 0
        self.assertEqual(details["score"], 82)

    def test_analysis_snapshot_hash_and_equality_preserve_json_types(self) -> None:
        common: dict[str, object] = {
            "snapshot_id": "snapshot-001",
            "change_set_id": "change-set-001",
            "athlete_id": "athlete-001",
            "period": "2026-10-26",
            "risk_level": "low",
            "created_at": self.now,
            "analysis_kind": "daily",
            "algorithm_version": "wellness/1.0",
        }
        boolean_snapshot = AnalysisSnapshot(
            **common,  # type: ignore[arg-type]
            report={"period": "2026-10-26", "signal": True},
        )
        integer_snapshot = AnalysisSnapshot(
            **common,  # type: ignore[arg-type]
            report={"period": "2026-10-26", "signal": 1},
        )

        self.assertIsInstance(hash(boolean_snapshot), int)
        self.assertIsInstance(hash(integer_snapshot), int)
        self.assertNotEqual(boolean_snapshot, integer_snapshot)
        self.assertEqual(len({boolean_snapshot, integer_snapshot}), 2)

        matching_snapshot = AnalysisSnapshot(
            **common,  # type: ignore[arg-type]
            report={"period": "2026-10-26", "signal": True},
        )
        frozen_object = boolean_snapshot.report
        matching_object = matching_snapshot.report
        self.assertEqual(frozen_object, matching_object)
        self.assertEqual(hash(frozen_object), hash(matching_object))
        self.assertNotEqual(frozen_object, {"period": "2026-10-26", "signal": True})

        array_snapshot = AnalysisSnapshot(
            **common,  # type: ignore[arg-type]
            report={"period": "2026-10-26", "signals": [True, 1]},
        )
        matching_array_snapshot = AnalysisSnapshot(
            **common,  # type: ignore[arg-type]
            report={"period": "2026-10-26", "signals": [True, 1]},
        )
        frozen_array = array_snapshot.report["signals"]
        matching_array = matching_array_snapshot.report["signals"]
        self.assertEqual(frozen_array, matching_array)
        self.assertEqual(hash(frozen_array), hash(matching_array))
        self.assertNotEqual(frozen_array, (True, 1))

    def test_notification_delivery_attempts_require_a_non_bool_non_negative_integer(self) -> None:
        values: tuple[object, ...] = (True, 1.5, -1)
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                UserNotification(
                    notification_id="notification-001",
                    snapshot_id="snapshot-001",
                    athlete_id="athlete-001",
                    priority="digest",
                    title="Training analysis ready",
                    body="Open the product to review your analysis.",
                    deliver_after=None,
                    created_at=self.now,
                    delivery_attempts=value,  # type: ignore[arg-type]
                )

    def test_database_operations_do_not_leak_connections(self) -> None:
        with TemporaryDirectory() as directory, warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            repository = SQLiteAutomationRepository(Path(directory) / "athlete.sqlite3")
            repository.list_subscriptions()
            del repository
            gc.collect()

        leaked = [warning for warning in caught if "unclosed database" in str(warning.message)]
        self.assertEqual(leaked, [])


if __name__ == "__main__":
    unittest.main()
