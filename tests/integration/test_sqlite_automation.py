from __future__ import annotations

import gc
import sqlite3
import subprocess
import sys
import unittest
import warnings
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from athlete_growth.adapters.storage.sqlite_automation import SQLiteAutomationRepository
from athlete_growth.application.automation import (
    AnalysisSnapshot,
    NotificationMode,
    SyncSubscription,
    UserNotification,
)
from athlete_growth.application.ingest.service import IngestionResult
from athlete_growth.domain.records import JsonValue


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
            change_set = repository.save_change_set(
                run.run_id,
                self.result,
                created_at=self.now,
            )
            snapshot = repository.save_snapshot(
                change_set,
                report_payload,
                "low",
                created_at=self.now,
            )
            notification = repository.enqueue_notification(
                snapshot,
                priority="digest",
                deliver_after=later,
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

            first_change_set = repository.save_change_set(
                run.run_id,
                self.result,
                created_at=self.now,
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

            first_claim = first_repository.claim_due(self.now)
            second_claim = second_repository.claim_due(self.now)

            self.assertEqual(len(first_claim), 1)
            self.assertEqual(second_claim, [])
            claimed_subscription, claimed_run = first_claim[0]
            self.assertEqual(claimed_subscription, self.subscription)
            self.assertIsNotNone(claimed_run.request_key)
            self.assertGreater(
                first_repository.get_next_run_at("athlete-001", "garmin-cn"),
                self.now,
            )

            reconstructed = SQLiteAutomationRepository(database_file)
            self.assertEqual(reconstructed.claim_due(self.now), [])
            self.assertEqual(
                reconstructed.latest_sync_run("athlete-001", "garmin-cn"),
                claimed_run,
            )

            reconstructed.save_subscription(self.subscription, next_run_at=self.now)
            replayed_claim = reconstructed.claim_due(self.now)
            self.assertEqual(replayed_claim, [(self.subscription, claimed_run)])
            with closing(sqlite3.connect(database_file)) as connection:
                run_count = connection.execute(
                    "SELECT COUNT(*) FROM automation_sync_runs WHERE request_key = ?",
                    (claimed_run.request_key,),
                ).fetchone()[0]
            self.assertEqual(run_count, 1)

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
            change_set = repository.save_change_set(
                run.run_id,
                self.result,
                created_at=self.now,
            )
            snapshot = repository.save_snapshot(
                change_set,
                {"period": "2026-10-26"},
                "moderate",
                created_at=self.now,
            )
            notification = repository.enqueue_notification(
                snapshot,
                priority="actionable",
                deliver_after=None,
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
            failed = repository.fail_sync_run(
                run.run_id,
                error="authentication expired",
                completed_at=failed_at,
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
            snapshot.report["period"] = "changed"
        with self.assertRaises(TypeError):
            details["score"] = 0
        with self.assertRaises(TypeError):
            signals.append("changed")
        with self.assertRaises(TypeError):
            first_signal["value"] = 0

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
