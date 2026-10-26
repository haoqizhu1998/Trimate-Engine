from __future__ import annotations

import gc
import unittest
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from athlete_growth.adapters.storage.sqlite_automation import SQLiteAutomationRepository
from athlete_growth.application.automation import (
    NotificationMode,
    SyncSubscription,
)
from athlete_growth.application.ingest.service import IngestionResult


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
            run = repository.start_sync_run(self.subscription, now=self.now, reason="schedule")

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
                deliver_after=self.now + timedelta(hours=1),
            )

            self.assertEqual(replayed_change_set, first_change_set)
            self.assertEqual(replayed_snapshot, first_snapshot)
            self.assertEqual(replayed_notification, first_notification)
            self.assertEqual(repository.list_snapshots("athlete-001"), [first_snapshot])
            self.assertEqual(repository.list_unread("athlete-001"), [first_notification])

    def test_read_and_delivery_state_are_durable(self) -> None:
        with TemporaryDirectory() as directory:
            database_file = Path(directory) / "athlete.sqlite3"
            repository = SQLiteAutomationRepository(database_file)
            run = repository.start_sync_run(self.subscription, now=self.now, reason="schedule")
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
            run = repository.start_sync_run(self.subscription, now=self.now, reason="schedule")
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
