from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from athlete_growth.adapters.notifications.macos import MacOSNotificationSink
from athlete_growth.adapters.storage import (
    SQLiteAthleteProfileRepository,
    SQLiteAutomationRepository,
    SQLiteRecordStore,
)
from athlete_growth.application.automation import (
    AutomaticTrainingLoop,
    NotificationMode,
    SyncSubscription,
)
from athlete_growth.application.ingest.service import IngestionService
from athlete_growth.domain.athlete import AthleteProfile
from athlete_growth.ports.data_source import (
    HealthStatus,
    RawSourceRecord,
    SourceCapabilities,
    SyncBatch,
    SyncWindow,
)


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class MutableConnector:
    def __init__(self, source: str, records: tuple[RawSourceRecord, ...]) -> None:
        self.source = source
        self.records = records
        self.error: Exception | None = None
        self.windows: list[SyncWindow] = []

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(self.source, None, ("daily-summary", "check-in"))

    def sync(self, window: SyncWindow, cursor: str | None) -> SyncBatch:
        del cursor
        self.windows.append(window)
        if self.error is not None:
            raise self.error
        return SyncBatch(self.records)

    def healthcheck(self) -> HealthStatus:
        return HealthStatus(True, "ready")

    def revoke(self) -> None:
        return None


class RecordingSink:
    def __init__(self) -> None:
        self.delivered = []

    def deliver(self, notification) -> None:
        self.delivered.append(notification)


class FailsOnceSink(RecordingSink):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def deliver(self, notification) -> None:
        self.calls += 1
        if self.calls == 1:
            raise OSError("notification center unavailable")
        super().deliver(notification)


def _record(
    source: str,
    kind: str,
    source_id: str,
    *,
    occurred_at: datetime,
    **payload,
) -> RawSourceRecord:
    return RawSourceRecord(
        source=source,
        kind=kind,
        source_id=source_id,
        occurred_at=occurred_at,
        payload=payload,
        canonical_payload=payload,
    )


class AutomaticTrainingLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.database_file = self.root / "athlete.sqlite3"
        self.records = SQLiteRecordStore(self.database_file)
        self.automation = SQLiteAutomationRepository(self.database_file)
        self.profiles = SQLiteAthleteProfileRepository(self.database_file)
        self.clock = MutableClock(datetime(2026, 10, 26, 12, 0, tzinfo=UTC))

    def add_profile(self, athlete_id: str = "athlete-001") -> None:
        self.profiles.save(
            AthleteProfile(
                athlete_id=athlete_id,
                display_name=athlete_id,
                timezone_name="Asia/Shanghai",
                unit_system="metric",
                created_at=self.clock(),
            )
        )

    def add_subscription(
        self,
        source: str,
        *,
        athlete_id: str = "athlete-001",
        mode: NotificationMode = NotificationMode.ACTIONABLE_DIGEST,
        interval_minutes: int = 30,
    ) -> SyncSubscription:
        subscription = SyncSubscription(
            athlete_id=athlete_id,
            source=source,
            interval_minutes=interval_minutes,
            lookback_days=3,
            notification_mode=mode,
            digest_hour_local=23,
            enabled=True,
        )
        self.automation.save_subscription(subscription, next_run_at=self.clock())
        return subscription

    def loop(self, connectors, sink=None) -> AutomaticTrainingLoop:
        return AutomaticTrainingLoop(
            automation_repository=self.automation,
            record_store=self.records,
            profile_repository=self.profiles,
            connectors=connectors,
            raw_root=self.root / "raw",
            notification_sink=sink or RecordingSink(),
            clock=self.clock,
            owner="integration-worker",
            lease_duration=timedelta(minutes=5),
        )

    def table_count(self, table: str) -> int:
        with closing(sqlite3.connect(self.database_file)) as connection:
            row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        assert row is not None
        return int(row[0])

    def test_real_change_runs_reports_once_while_duplicates_are_quiet(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        occurred_at = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "daily-summary",
                    "summary-1",
                    occurred_at=occurred_at,
                    calendar_date="2026-10-26",
                    steps=8000,
                ),
                _record(
                    "watch",
                    "check-in",
                    "check-in-1",
                    occurred_at=occurred_at,
                    pain=0,
                    fatigue=2,
                    soreness=1,
                    illness=False,
                ),
            ),
        )
        sink = RecordingSink()
        loop = self.loop({"watch": connector}, sink)

        first = loop.run_once()

        self.assertEqual((first.completed, first.changed, first.failed), (1, 1, 0))
        self.assertEqual(self.table_count("data_change_sets"), 1)
        snapshots = self.automation.list_snapshots("athlete-001")
        self.assertEqual({item.analysis_kind for item in snapshots}, {"daily", "weekly"})
        daily = next(item for item in snapshots if item.analysis_kind == "daily")
        self.assertEqual(daily.algorithm_version, "wellness-report/1.0")
        self.assertEqual(daily.risk_level, "low")
        unread = self.automation.list_unread("athlete-001")
        self.assertEqual(len(unread), 1)
        self.assertEqual(unread[0].priority, "digest")
        self.assertGreater(unread[0].deliver_after, self.clock())
        self.assertEqual(sink.delivered, [])
        self.assertEqual(
            connector.windows[0],
            SyncWindow(date(2026, 10, 24), date(2026, 10, 26)),
        )

        self.clock.now += timedelta(minutes=30)
        second = loop.run_once()

        self.assertEqual((second.completed, second.no_changes), (1, 1))
        self.assertEqual(self.table_count("data_change_sets"), 1)
        self.assertEqual(len(self.automation.list_snapshots("athlete-001")), 2)
        self.assertEqual(len(self.automation.list_unread("athlete-001")), 1)

        connector.records = (
            _record(
                "watch",
                "daily-summary",
                "summary-1",
                occurred_at=occurred_at,
                calendar_date="2026-10-26",
                steps=10000,
            ),
            connector.records[1],
        )
        self.clock.now += timedelta(minutes=30)
        third = loop.run_once()

        self.assertEqual((third.completed, third.changed), (1, 1))
        self.assertEqual(self.table_count("data_change_sets"), 2)
        self.assertEqual(len(self.automation.list_snapshots("athlete-001")), 4)
        self.assertEqual(len(self.automation.list_unread("athlete-001")), 2)

    def test_connector_failure_is_durable_and_other_due_work_continues(self) -> None:
        self.add_profile()
        self.add_subscription("a-failing")
        self.add_subscription("b-working")
        failed = MutableConnector(
            "a-failing",
            (
                _record(
                    "a-failing",
                    "daily-summary",
                    "existing",
                    occurred_at=datetime(2026, 10, 25, 8, 0, tzinfo=UTC),
                    calendar_date="2026-10-25",
                    steps=7000,
                ),
            ),
        )
        existing = IngestionService(
            athlete_id="athlete-001",
            store=self.records,
            raw_dir=self.root / "raw" / "athlete-001",
        ).sync(failed, SyncWindow(date(2026, 10, 24), date(2026, 10, 26)))
        failed.error = ConnectionError("private credential should not escape")
        working = MutableConnector(
            "b-working",
            (
                _record(
                    "b-working",
                    "check-in",
                    "ok",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                    fatigue=1,
                    soreness=1,
                    illness=False,
                ),
            ),
        )

        result = self.loop({"a-failing": failed, "b-working": working}).run_once()

        self.assertEqual((result.completed, result.failed), (1, 1))
        failed_run = self.automation.latest_sync_run("athlete-001", "a-failing")
        working_run = self.automation.latest_sync_run("athlete-001", "b-working")
        assert failed_run is not None and working_run is not None
        self.assertEqual(failed_run.status, "failed")
        self.assertNotIn("credential", failed_run.error or "")
        self.assertEqual(working_run.status, "completed")
        current_ids = {record.record_id for record in self.records.list_current("athlete-001")}
        self.assertEqual(len(current_ids), 2)
        self.assertIn(existing.changed_record_ids[0], current_ids)

    def test_notification_modes_delivery_and_retry_are_durable(self) -> None:
        connectors = {}
        cases = {
            "actionable-moderate": (NotificationMode.ACTIONABLE_DIGEST, 5),
            "actionable-high": (NotificationMode.ACTIONABLE_DIGEST, 7),
            "all": (NotificationMode.ALL, 0),
            "off": (NotificationMode.OFF, 7),
        }
        for name, (mode, pain) in cases.items():
            athlete_id = f"athlete-{name}"
            source = f"source-{name}"
            self.add_profile(athlete_id)
            self.add_subscription(source, athlete_id=athlete_id, mode=mode)
            connectors[source] = MutableConnector(
                source,
                (
                    _record(
                        source,
                        "check-in",
                        name,
                        occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                        pain=pain,
                        fatigue=2,
                        soreness=2,
                        illness=False,
                    ),
                ),
            )
        sink = RecordingSink()

        result = self.loop(connectors, sink).run_once()

        self.assertEqual((result.completed, result.changed), (4, 4))
        self.assertEqual(len(sink.delivered), 3)
        self.assertEqual(
            {item.athlete_id for item in sink.delivered},
            {"athlete-actionable-moderate", "athlete-actionable-high", "athlete-all"},
        )
        moderate = self.automation.list_unread("athlete-actionable-moderate")[0]
        high = self.automation.list_unread("athlete-actionable-high")[0]
        all_notification = self.automation.list_unread("athlete-all")[0]
        self.assertEqual(moderate.priority, "actionable")
        self.assertEqual(high.priority, "actionable")
        self.assertIsNone(moderate.deliver_after)
        self.assertIsNone(high.deliver_after)
        self.assertIsNone(all_notification.deliver_after)
        self.assertEqual(self.automation.list_unread("athlete-off"), [])
        self.assertEqual(len(self.automation.list_snapshots("athlete-off")), 2)

        retry_root = self.root / "retry"
        retry_database = retry_root / "athlete.sqlite3"
        retry_records = SQLiteRecordStore(retry_database)
        retry_automation = SQLiteAutomationRepository(retry_database)
        retry_profiles = SQLiteAthleteProfileRepository(retry_database)
        retry_profiles.save(
            AthleteProfile("retry-athlete", "Retry", "Asia/Shanghai", "metric", self.clock())
        )
        subscription = SyncSubscription(
            "retry-athlete", "retry-source", 30, 1, NotificationMode.ALL, 23, True
        )
        retry_automation.save_subscription(subscription, next_run_at=self.clock())
        retry_connector = MutableConnector(
            "retry-source",
            (
                _record(
                    "retry-source",
                    "check-in",
                    "retry",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
        )
        flaky = FailsOnceSink()
        retry_loop = AutomaticTrainingLoop(
            automation_repository=retry_automation,
            record_store=retry_records,
            profile_repository=retry_profiles,
            connectors={"retry-source": retry_connector},
            raw_root=retry_root / "raw",
            notification_sink=flaky,
            clock=self.clock,
            owner="retry-worker",
        )

        first_delivery = retry_loop.run_once()
        pending = retry_automation.list_pending(self.clock())
        self.assertEqual((first_delivery.delivery_failures, len(pending)), (1, 1))
        self.assertEqual(pending[0].delivery_attempts, 1)
        self.assertIn("OSError", pending[0].last_error or "")

        second_delivery = retry_loop.deliver_pending()
        delivered = retry_automation.get_notification(pending[0].notification_id)
        assert delivered is not None
        self.assertEqual((second_delivery.delivered, second_delivery.failed), (1, 0))
        self.assertIsNotNone(delivered.delivered_at)
        self.assertEqual(delivered.delivery_attempts, 2)

    def test_expired_claim_is_recovered_with_same_run_id(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        old_claim = self.automation.claim_due(
            self.clock(),
            owner="crashed-worker",
            lease_duration=timedelta(minutes=1),
        )[0][1]
        old_token = old_claim.lease_token
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "recovered",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
        )
        self.clock.now += timedelta(minutes=2)
        reclaimed = self.automation.reclaim_sync_run(
            old_claim.run_id,
            now=self.clock(),
            owner="second-crashed-worker",
            lease_duration=timedelta(minutes=1),
        )
        with self.assertRaisesRegex(ValueError, "lease token conflict"):
            self.automation.complete_sync_run(
                old_claim.run_id,
                completed_at=self.clock(),
                lease_token=old_token,
            )
        self.clock.now += timedelta(minutes=2)

        result = self.loop({"watch": connector}).run_once()

        recovered = self.automation.get_sync_run(old_claim.run_id)
        assert recovered is not None
        self.assertEqual((result.recovered, result.completed), (1, 1))
        self.assertEqual(recovered.run_id, old_claim.run_id)
        self.assertEqual(recovered.status, "completed")
        self.assertNotEqual(old_token, reclaimed.lease_token)

    def test_recovery_resumes_analysis_from_an_already_persisted_change_set(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "persisted-before-crash",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
        )
        crashed_run = self.automation.claim_due(
            self.clock(),
            owner="crashed-after-change-set",
            lease_duration=timedelta(minutes=1),
        )[0][1]
        assert crashed_run.lease_token is not None
        ingestion = IngestionService(
            athlete_id="athlete-001",
            store=self.records,
            raw_dir=self.root / "raw" / "athlete-001",
        )
        ingestion_result = ingestion.sync(
            connector,
            SyncWindow(date(2026, 10, 24), date(2026, 10, 26)),
        )
        change_set = self.automation.save_change_set(
            crashed_run.run_id,
            ingestion_result,
            created_at=self.clock(),
            lease_token=crashed_run.lease_token,
        )
        self.clock.now += timedelta(minutes=2)

        result = self.loop({"watch": connector}).run_once()

        finished = self.automation.get_sync_run(crashed_run.run_id)
        assert finished is not None
        self.assertEqual((result.recovered, result.completed, result.changed), (1, 1, 1))
        self.assertEqual(finished.status, "completed")
        self.assertEqual(len(self.automation.list_snapshots("athlete-001")), 2)
        self.assertEqual(len(self.automation.list_unread("athlete-001")), 1)
        self.assertEqual(connector.windows, [SyncWindow(date(2026, 10, 24), date(2026, 10, 26))])
        self.assertEqual(change_set.run_id, crashed_run.run_id)

    def test_recovery_after_notification_keeps_original_digest_schedule(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "persisted-before-completion",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
        )
        loop = self.loop({"watch": connector})

        with (
            patch.object(
                self.automation,
                "complete_sync_run",
                side_effect=SystemExit("simulated process crash"),
            ),
            self.assertRaisesRegex(SystemExit, "simulated process crash"),
        ):
            loop.run_once()

        crashed_run = self.automation.latest_sync_run("athlete-001", "watch")
        assert crashed_run is not None
        original = self.automation.list_unread("athlete-001")[0]
        self.assertEqual(crashed_run.status, "running")
        self.assertEqual(original.deliver_after, datetime(2026, 10, 26, 15, 0, tzinfo=UTC))

        self.clock.now += timedelta(days=1, hours=5)
        result = loop.run_once()

        recovered = self.automation.get_sync_run(crashed_run.run_id)
        notification = self.automation.get_notification(original.notification_id)
        assert recovered is not None and notification is not None
        self.assertEqual((result.recovered, result.completed, result.failed), (1, 1, 0))
        self.assertEqual(recovered.status, "completed")
        self.assertEqual(notification.deliver_after, original.deliver_after)


class MacOSNotificationSinkTests(unittest.TestCase):
    def test_passes_notification_text_as_argv_without_shell_interpolation(self) -> None:
        from athlete_growth.application.automation import UserNotification

        notification = UserNotification(
            notification_id="notification-1",
            snapshot_id="snapshot-1",
            athlete_id="athlete-1",
            priority="actionable",
            title='Title "$(touch /tmp/nope)"',
            body="Body; rm -rf /",
            deliver_after=None,
            created_at=datetime(2026, 10, 26, 12, 0, tzinfo=UTC),
        )

        with patch("athlete_growth.adapters.notifications.macos.subprocess.run") as run:
            MacOSNotificationSink(timeout_seconds=4).deliver(notification)

        args, kwargs = run.call_args
        argv = args[0]
        self.assertEqual(argv[0], "/usr/bin/osascript")
        self.assertEqual(argv[-2:], [notification.title, notification.body])
        self.assertNotIn(notification.title, argv[2])
        self.assertNotIn(notification.body, argv[2])
        self.assertEqual(kwargs["shell"], False)
        self.assertEqual(kwargs["timeout"], 4)
        self.assertEqual(kwargs["check"], True)


if __name__ == "__main__":
    unittest.main()
