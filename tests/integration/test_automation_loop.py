from __future__ import annotations

import shutil
import sqlite3
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Thread
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
from athlete_growth.application.check_in.service import CheckInService
from athlete_growth.application.ingest.service import IngestionService
from athlete_growth.application.report.service import ReportService
from athlete_growth.domain.athlete import AthleteProfile
from athlete_growth.domain.records import CanonicalRecord
from athlete_growth.domain.safety import RiskLevel
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
    def __init__(
        self,
        source: str,
        records: tuple[RawSourceRecord, ...],
        *,
        after_fetch: Callable[[], None] | None = None,
        next_cursor: str | None = None,
    ) -> None:
        self.source = source
        self.records = records
        self.after_fetch = after_fetch
        self.next_cursor = next_cursor
        self.error: Exception | None = None
        self.windows: list[SyncWindow] = []
        self.cursors: list[str | None] = []

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(self.source, None, ("daily-summary", "check-in"))

    def sync(self, window: SyncWindow, cursor: str | None) -> SyncBatch:
        self.windows.append(window)
        self.cursors.append(cursor)
        if self.error is not None:
            raise self.error
        batch = SyncBatch(self.records, self.next_cursor)
        if self.after_fetch is not None:
            self.after_fetch()
        return batch

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


class FailsOnceConnector(MutableConnector):
    def __init__(self, source: str, records: tuple[RawSourceRecord, ...]) -> None:
        super().__init__(source, records)
        self.attempts = 0

    def sync(self, window: SyncWindow, cursor: str | None) -> SyncBatch:
        self.attempts += 1
        if self.attempts == 1:
            self.windows.append(window)
            self.cursors.append(cursor)
            raise ConnectionError("temporary connector outage")
        return super().sync(window, cursor)


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

    def test_new_activity_in_approved_plan_week_prompts_review(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        self.records.save(
            CanonicalRecord.create(
                athlete_id="athlete-001",
                kind="training-plan",
                occurred_at=self.clock(),
                data={
                    "plan_id": "synthetic-plan",
                    "goal_id": "synthetic-goal",
                    "version": 1,
                    "status": "approved",
                    "start_date": "2026-10-26",
                    "sessions": [],
                },
            )
        )
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "activity",
                    "new-training",
                    occurred_at=datetime(2026, 10, 26, 9, tzinfo=UTC),
                    activity_type="running",
                    duration_seconds=1800,
                ),
            ),
        )
        loop = self.loop({"watch": connector})
        loop.run_once()
        notice = self.automation.list_unread("athlete-001")[0]
        self.assertEqual(notice.priority, "actionable")
        self.assertEqual(notice.title, "Training plan needs review")
        self.assertIn("Review possible session matches", notice.body)
        self.clock.now += timedelta(minutes=30)
        loop.run_once()
        self.assertEqual(len(self.automation.list_unread("athlete-001")), 1)

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

    def crash_after_raw_change_set(
        self,
        source_id: str,
    ) -> tuple[AutomaticTrainingLoop, str, Path]:
        self.add_profile()
        self.add_subscription("watch")
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    source_id,
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
        )
        loop = self.loop({"watch": connector})
        with (
            patch.object(
                self.automation,
                "save_snapshot",
                side_effect=SystemExit("simulated crash after change set"),
            ),
            self.assertRaisesRegex(SystemExit, "after change set"),
        ):
            loop.run_once(limit=1)
        crashed_run = self.automation.latest_sync_run("athlete-001", "watch")
        assert crashed_run is not None
        raw_files = list((self.root / "raw" / "athlete-001").rglob("*.json"))
        self.assertEqual(len(raw_files), 1)
        return loop, crashed_run.run_id, raw_files[0]

    def table_count(self, table: str) -> int:
        with closing(sqlite3.connect(self.database_file)) as connection:
            row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        assert row is not None
        return int(row[0])

    def set_notification_mode(
        self,
        source: str,
        mode: NotificationMode,
        *,
        athlete_id: str = "athlete-001",
    ) -> None:
        subscription = self.automation.get_subscription(athlete_id, source)
        next_run_at = self.automation.get_next_run_at(athlete_id, source)
        assert subscription is not None and next_run_at is not None
        self.automation.save_subscription(
            replace(subscription, notification_mode=mode),
            next_run_at=next_run_at,
        )

    def queue_future_digest(self, source: str = "watch") -> str:
        self.add_profile()
        self.add_subscription(source)
        connector = MutableConnector(
            source,
            (
                _record(
                    source,
                    "check-in",
                    "queued-digest",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
        )

        result = self.loop({source: connector}).run_once(limit=1)

        self.assertEqual((result.notifications, result.delivered), (1, 0))
        notification = self.automation.list_unread("athlete-001")[0]
        self.assertEqual(notification.deliver_after, datetime(2026, 10, 26, 15, 0, tzinfo=UTC))
        return notification.notification_id

    def test_loop_rejects_record_store_from_a_different_database(self) -> None:
        other_records = SQLiteRecordStore(self.root / "other.sqlite3")

        with self.assertRaisesRegex(ValueError, "same database"):
            AutomaticTrainingLoop(
                automation_repository=self.automation,
                record_store=other_records,
                profile_repository=self.profiles,
                connectors={},
                raw_root=self.root / "raw",
            )

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
        first_run = self.automation.latest_sync_run("athlete-001", "watch")
        assert first_run is not None
        first_change_set = self.automation.get_change_set_for_run(first_run.run_id)
        assert first_change_set is not None
        self.assertEqual(
            first_change_set.projection_record_ids,
            tuple(record.record_id for record in self.records.list_current("athlete-001")),
        )
        snapshots = self.automation.list_snapshots("athlete-001")
        self.assertEqual({item.analysis_kind for item in snapshots}, {"daily", "weekly"})
        daily = next(item for item in snapshots if item.analysis_kind == "daily")
        self.assertEqual(daily.algorithm_version, "wellness-report/1.1")
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

    def test_utc_record_uses_athlete_local_date_for_actionable_analysis(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "local-morning-high",
                    occurred_at=datetime(2026, 10, 25, 16, 30, tzinfo=UTC),
                    pain=8,
                    fatigue=2,
                    soreness=2,
                    illness=False,
                ),
            ),
        )
        sink = RecordingSink()

        result = self.loop({"watch": connector}, sink).run_once(limit=1)

        snapshots = self.automation.list_snapshots("athlete-001")
        daily = next(item for item in snapshots if item.analysis_kind == "daily")
        notification = self.automation.list_unread("athlete-001")[0]
        self.assertEqual((result.completed, result.failed), (1, 0))
        self.assertEqual((daily.period, daily.risk_level), ("2026-10-26", "high"))
        self.assertEqual(notification.priority, "actionable")
        self.assertIsNone(notification.deliver_after)
        self.assertEqual(len(sink.delivered), 1)

    def test_activity_sync_allows_evidence_free_manual_check_in_in_projection(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        manual_check_in = CheckInService("athlete-001", self.records).record(
            occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
            fatigue=2,
            soreness=1,
            pain=8,
            mood=7,
            motivation=6,
            illness=False,
        )
        self.assertEqual(manual_check_in.evidence, ())
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "activity",
                    "activity-with-manual-context",
                    occurred_at=datetime(2026, 10, 26, 9, 0, tzinfo=UTC),
                    activity_type="running",
                    duration_seconds=1800,
                    distance_meters=5000,
                ),
            ),
        )
        sink = RecordingSink()

        result = self.loop({"watch": connector}, sink).run_once(limit=1)

        run = self.automation.latest_sync_run("athlete-001", "watch")
        assert run is not None
        change_set = self.automation.get_change_set_for_run(run.run_id)
        assert change_set is not None
        snapshots = self.automation.list_snapshots("athlete-001")
        daily = next(item for item in snapshots if item.analysis_kind == "daily")
        self.assertEqual((result.completed, result.changed, result.failed), (1, 1, 0))
        self.assertIn(manual_check_in.record_id, change_set.projection_record_ids)
        self.assertNotIn(manual_check_in.record_id, change_set.record_ids)
        self.assertEqual({item.analysis_kind for item in snapshots}, {"daily", "weekly"})
        self.assertEqual(daily.risk_level, "high")
        self.assertEqual(len(self.automation.list_unread("athlete-001")), 1)
        self.assertEqual(len(sink.delivered), 1)

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

    def test_batch_source_mismatch_fails_without_raw_or_canonical_mutation(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "other-source",
                    "check-in",
                    "mismatched-source",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
        )

        result = self.loop({"watch": connector}).run_once(limit=1)

        run = self.automation.latest_sync_run("athlete-001", "watch")
        assert run is not None
        self.assertEqual((result.completed, result.failed), (0, 1))
        self.assertEqual(run.status, "failed")
        self.assertEqual(self.records.list_current("athlete-001"), [])
        self.assertEqual(self.table_count("data_change_sets"), 0)
        raw_root = self.root / "raw" / "athlete-001"
        self.assertFalse(raw_root.exists() and any(raw_root.rglob("*.json")))

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
        notification = retry_automation.list_unread("retry-athlete")[0]
        failed_notification = retry_automation.get_notification(notification.notification_id)
        assert failed_notification is not None
        self.assertEqual(first_delivery.delivery_failures, 1)
        self.assertEqual(retry_automation.list_pending(self.clock()), [])
        self.assertEqual(failed_notification.delivery_attempts, 1)
        self.assertIn("OSError", failed_notification.last_error or "")

        second_delivery = retry_loop.deliver_pending()
        self.assertEqual(second_delivery.attempted, 0)
        self.clock.now += timedelta(minutes=1)
        third_delivery = retry_loop.deliver_pending()
        delivered = retry_automation.get_notification(notification.notification_id)
        assert delivered is not None
        self.assertEqual((third_delivery.delivered, third_delivery.failed), (1, 0))
        self.assertIsNotNone(delivered.delivered_at)
        self.assertEqual(delivered.delivery_attempts, 2)

    def test_off_policy_suppresses_an_existing_due_digest_without_deleting_it(self) -> None:
        notification_id = self.queue_future_digest()
        self.set_notification_mode("watch", NotificationMode.OFF)
        self.clock.now = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)
        sink = RecordingSink()

        summary = self.loop({}, sink).deliver_pending()

        notification = self.automation.get_notification(notification_id)
        assert notification is not None
        self.assertEqual((summary.attempted, summary.delivered, summary.failed), (0, 0, 0))
        self.assertEqual(sink.delivered, [])
        self.assertIsNone(notification.delivered_at)
        self.assertEqual(
            [item.notification_id for item in self.automation.list_unread("athlete-001")],
            [notification_id],
        )
        self.assertEqual(
            [item.notification_id for item in self.automation.list_pending(self.clock())],
            [notification_id],
        )

    def test_policy_flip_after_fetch_suppresses_notification_creation(self) -> None:
        self.add_profile()
        self.add_subscription("watch")

        def disable_notifications() -> None:
            self.set_notification_mode("watch", NotificationMode.OFF)

        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "disabled-during-sync",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
            after_fetch=disable_notifications,
        )

        summary = self.loop({"watch": connector}).run_once(limit=1)

        self.assertEqual((summary.completed, summary.changed), (1, 1))
        self.assertEqual(summary.snapshots, 2)
        self.assertEqual(summary.notifications, 0)
        self.assertEqual(self.automation.list_unread("athlete-001"), [])

    def test_off_policy_releases_a_claim_if_configuration_changes_before_sink(self) -> None:
        notification_id = self.queue_future_digest()
        self.clock.now = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)
        parent = self

        class DisableAfterClaimRepository(SQLiteAutomationRepository):
            disabled = False

            def claim_pending(self, *args, **kwargs):
                claimed = super().claim_pending(*args, **kwargs)
                if claimed and not self.disabled:
                    self.disabled = True
                    parent.set_notification_mode("watch", NotificationMode.OFF)
                return claimed

        repository = DisableAfterClaimRepository(self.database_file)
        sink = RecordingSink()
        loop = AutomaticTrainingLoop(
            automation_repository=repository,
            record_store=SQLiteRecordStore(self.database_file),
            profile_repository=self.profiles,
            connectors={},
            raw_root=self.root / "raw",
            notification_sink=sink,
            clock=self.clock,
            owner="policy-race-worker",
        )

        summary = loop.deliver_pending(limit=1)

        notification = repository.get_notification(notification_id)
        assert notification is not None
        self.assertEqual((summary.attempted, summary.delivered, summary.failed), (1, 0, 0))
        self.assertEqual(sink.delivered, [])
        self.assertIsNone(notification.delivered_at)
        self.assertIsNone(notification.delivery_lease_token)
        self.assertIsNone(notification.delivery_lease_owner)
        self.assertIsNone(notification.delivery_lease_expires_at)

    def test_configure_off_waits_for_validated_delivery_transaction(self) -> None:
        notification_id = self.queue_future_digest()
        self.clock.now = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)
        sink_started = Event()
        release_sink = Event()

        class BlockingSink(RecordingSink):
            def deliver(self, notification) -> None:
                sink_started.set()
                if not release_sink.wait(timeout=2):
                    raise TimeoutError("test sink release timed out")
                super().deliver(notification)

        sink = BlockingSink()
        delivery_summaries = []
        delivery_errors = []

        def deliver() -> None:
            try:
                delivery_summaries.append(self.loop({}, sink).deliver_pending(limit=1))
            except BaseException as exc:
                delivery_errors.append(exc)

        delivery_thread = Thread(target=deliver)
        delivery_thread.start()
        self.assertTrue(sink_started.wait(timeout=1))

        configure_started = Event()
        configured = Event()

        def configure_off() -> None:
            configure_started.set()
            self.set_notification_mode("watch", NotificationMode.OFF)
            configured.set()

        configure_thread = Thread(target=configure_off)
        configure_thread.start()
        self.assertTrue(configure_started.wait(timeout=1))
        self.assertFalse(configured.wait(timeout=0.1))

        release_sink.set()
        delivery_thread.join(timeout=2)
        configure_thread.join(timeout=2)

        self.assertFalse(delivery_thread.is_alive())
        self.assertFalse(configure_thread.is_alive())
        self.assertEqual(delivery_errors, [])
        self.assertTrue(configured.is_set())
        self.assertEqual(len(delivery_summaries), 1)
        self.assertEqual(
            (
                delivery_summaries[0].attempted,
                delivery_summaries[0].delivered,
                delivery_summaries[0].failed,
            ),
            (1, 1, 0),
        )
        self.assertEqual(
            [item.notification_id for item in sink.delivered],
            [notification_id],
        )

    def test_reenabling_policy_makes_a_suppressed_digest_deliverable(self) -> None:
        notification_id = self.queue_future_digest()
        self.set_notification_mode("watch", NotificationMode.OFF)
        self.clock.now = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)
        self.assertEqual(
            self.automation.claim_pending(self.clock(), owner="suppressed-worker"),
            [],
        )
        self.set_notification_mode("watch", NotificationMode.ACTIONABLE_DIGEST)
        sink = RecordingSink()

        summary = self.loop({}, sink).deliver_pending()

        notification = self.automation.get_notification(notification_id)
        assert notification is not None
        self.assertEqual((summary.attempted, summary.delivered, summary.failed), (1, 1, 0))
        self.assertEqual(
            [item.notification_id for item in sink.delivered],
            [notification_id],
        )
        self.assertIsNotNone(notification.delivered_at)

    def test_notification_without_a_current_subscription_keeps_delivery_behavior(self) -> None:
        notification_id = self.queue_future_digest()
        with closing(sqlite3.connect(self.database_file)) as connection:
            connection.execute(
                "DELETE FROM sync_subscriptions WHERE athlete_id = ? AND source = ?",
                ("athlete-001", "watch"),
            )
            connection.commit()
        self.clock.now = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)
        sink = RecordingSink()

        summary = self.loop({}, sink).deliver_pending()

        self.assertEqual((summary.attempted, summary.delivered, summary.failed), (1, 1, 0))
        self.assertEqual(
            [item.notification_id for item in sink.delivered],
            [notification_id],
        )

    def test_delivery_failure_does_not_starve_the_next_notification(self) -> None:
        connectors = {}
        for athlete_id in ("athlete-a", "athlete-b"):
            source = f"source-{athlete_id}"
            self.add_profile(athlete_id)
            self.add_subscription(source, athlete_id=athlete_id)
            connectors[source] = MutableConnector(
                source,
                (
                    _record(
                        source,
                        "check-in",
                        athlete_id,
                        occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                        pain=0,
                    ),
                ),
            )
        self.loop(connectors).run_once(limit=2, delivery_limit=2)
        self.clock.now = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)
        oldest, next_notification = self.automation.list_pending(self.clock())
        sink = FailsOnceSink()
        delivery_loop = self.loop({}, sink)

        first = delivery_loop.deliver_pending(limit=1)
        second = delivery_loop.deliver_pending(limit=1)

        failed = self.automation.get_notification(oldest.notification_id)
        delivered = self.automation.get_notification(next_notification.notification_id)
        assert failed is not None and delivered is not None
        self.assertEqual((first.delivered, first.failed), (0, 1))
        self.assertEqual((second.delivered, second.failed), (1, 0))
        self.assertIsNone(failed.delivered_at)
        self.assertEqual(failed.delivery_attempts, 1)
        self.assertIsNotNone(delivered.delivered_at)
        self.assertEqual(sink.delivered[0].notification_id, next_notification.notification_id)
        self.assertEqual(self.automation.list_pending(self.clock()), [])

    def test_two_delivery_workers_call_the_sink_once(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "concurrent-delivery",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
        )
        self.loop({"watch": connector}).run_once(limit=1)
        self.clock.now = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)
        sink = RecordingSink()
        loops = tuple(
            AutomaticTrainingLoop(
                automation_repository=SQLiteAutomationRepository(self.database_file),
                record_store=SQLiteRecordStore(self.database_file),
                profile_repository=self.profiles,
                connectors={},
                raw_root=self.root / "raw",
                notification_sink=sink,
                clock=self.clock,
                owner=f"delivery-worker-{index}",
            )
            for index in range(2)
        )
        barrier = Barrier(2)

        def deliver(loop: AutomaticTrainingLoop):
            barrier.wait()
            return loop.deliver_pending(limit=1)

        with ThreadPoolExecutor(max_workers=2) as executor:
            summaries = list(executor.map(deliver, loops))

        self.assertEqual(len(sink.delivered), 1)
        self.assertEqual(sum(item.delivered for item in summaries), 1)

    def test_delivery_claims_each_notification_just_in_time(self) -> None:
        connectors = {}
        for athlete_id in ("athlete-a", "athlete-b"):
            source = f"source-{athlete_id}"
            self.add_profile(athlete_id)
            self.add_subscription(source, athlete_id=athlete_id)
            connectors[source] = MutableConnector(
                source,
                (
                    _record(
                        source,
                        "check-in",
                        athlete_id,
                        occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                        pain=0,
                    ),
                ),
            )
        self.loop(connectors).run_once(limit=2, delivery_limit=2)
        self.clock.now = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)

        parent_clock = self.clock

        class AdvancingReceiptRepository(SQLiteAutomationRepository):
            advanced = False

            def deliver_claimed(self, *args, **kwargs):
                receipt = super().deliver_claimed(*args, **kwargs)
                if not self.advanced:
                    self.advanced = True
                    parent_clock.now += timedelta(minutes=6)
                return receipt

        delivery_repository = AdvancingReceiptRepository(self.database_file)
        sink = RecordingSink()
        loop = AutomaticTrainingLoop(
            automation_repository=delivery_repository,
            record_store=SQLiteRecordStore(self.database_file),
            profile_repository=self.profiles,
            connectors={},
            raw_root=self.root / "raw",
            notification_sink=sink,
            clock=self.clock,
            owner="just-in-time-delivery",
            lease_duration=timedelta(minutes=5),
        )

        summary = loop.deliver_pending(limit=2)

        self.assertEqual((summary.attempted, summary.delivered, summary.failed), (2, 2, 0))
        self.assertEqual(len({item.notification_id for item in sink.delivered}), 2)
        self.assertTrue(
            all(
                notification.delivered_at is not None
                for athlete_id in ("athlete-a", "athlete-b")
                for notification in delivery_repository.list_unread(athlete_id)
            )
        )

    def test_expired_delivery_claim_is_rejected_before_sink_call(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "expires-before-delivery",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
        )
        self.loop({"watch": connector}).run_once(limit=1)
        self.clock.now = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)
        parent_clock = self.clock

        class ExpiringRepository(SQLiteAutomationRepository):
            def deliver_claimed(self, *args, **kwargs):
                parent_clock.now += timedelta(minutes=6)
                return super().deliver_claimed(*args, **kwargs)

        repository = ExpiringRepository(self.database_file)
        sink = RecordingSink()
        loop = AutomaticTrainingLoop(
            automation_repository=repository,
            record_store=SQLiteRecordStore(self.database_file),
            profile_repository=self.profiles,
            connectors={},
            raw_root=self.root / "raw",
            notification_sink=sink,
            clock=self.clock,
            owner="expiring-delivery-worker",
            lease_duration=timedelta(minutes=5),
        )

        summary = loop.deliver_pending(limit=1)

        notification = repository.list_unread("athlete-001")[0]
        self.assertEqual((summary.attempted, summary.delivered, summary.failed), (1, 0, 1))
        self.assertEqual(sink.delivered, [])
        self.assertIsNone(notification.delivered_at)

    def test_highest_risk_daily_snapshot_drives_one_immediate_notification(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "older-high",
                    occurred_at=datetime(2026, 10, 25, 8, 0, tzinfo=UTC),
                    pain=8,
                    fatigue=2,
                    soreness=2,
                    illness=False,
                ),
                _record(
                    "watch",
                    "check-in",
                    "newer-low",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                    fatigue=2,
                    soreness=2,
                    illness=False,
                ),
            ),
        )
        sink = RecordingSink()

        result = self.loop({"watch": connector}, sink).run_once()

        snapshots = self.automation.list_snapshots("athlete-001")
        daily = {item.period: item for item in snapshots if item.analysis_kind == "daily"}
        notifications = self.automation.list_unread("athlete-001")
        self.assertEqual((result.snapshots, result.notifications), (3, 1))
        self.assertEqual(daily["2026-10-25"].risk_level, "high")
        self.assertEqual(daily["2026-10-26"].risk_level, "low")
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].snapshot_id, daily["2026-10-25"].snapshot_id)
        self.assertEqual(notifications[0].priority, "actionable")
        self.assertIsNone(notifications[0].deliver_after)
        self.assertEqual(
            [item.notification_id for item in sink.delivered],
            [notifications[0].notification_id],
        )

    def test_weekly_snapshot_can_outrank_daily_for_notification(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "daily-low",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                    fatigue=2,
                    soreness=2,
                    illness=False,
                ),
            ),
        )
        real_weekly = ReportService.weekly

        def high_risk_weekly(service: ReportService, end_date: date):
            report = real_weekly(service, end_date)
            return replace(
                report,
                safety=replace(
                    report.safety,
                    level=RiskLevel.HIGH,
                    reasons=("weekly risk outranks daily",),
                    actions=("Review the weekly risk before training.",),
                ),
            )

        with patch.object(ReportService, "weekly", high_risk_weekly):
            self.loop({"watch": connector}).run_once()

        snapshots = self.automation.list_snapshots("athlete-001")
        daily = next(item for item in snapshots if item.analysis_kind == "daily")
        weekly = next(item for item in snapshots if item.analysis_kind == "weekly")
        notification = self.automation.list_unread("athlete-001")[0]
        self.assertEqual(daily.risk_level, "low")
        self.assertEqual(weekly.risk_level, "high")
        self.assertEqual(notification.snapshot_id, weekly.snapshot_id)
        self.assertEqual(notification.priority, "actionable")
        self.assertIsNone(notification.deliver_after)

    def test_equal_risk_prefers_latest_daily_snapshot_over_weekly(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        connector = MutableConnector(
            "watch",
            tuple(
                _record(
                    "watch",
                    "check-in",
                    f"high-{day}",
                    occurred_at=datetime(2026, 10, day, 8, 0, tzinfo=UTC),
                    pain=8,
                    fatigue=2,
                    soreness=2,
                    illness=False,
                )
                for day in (25, 26)
            ),
        )

        self.loop({"watch": connector}).run_once()

        snapshots = self.automation.list_snapshots("athlete-001")
        latest_daily = next(
            item
            for item in snapshots
            if item.analysis_kind == "daily" and item.period == "2026-10-26"
        )
        weekly = next(item for item in snapshots if item.analysis_kind == "weekly")
        notification = self.automation.list_unread("athlete-001")[0]
        self.assertEqual((latest_daily.risk_level, weekly.risk_level), ("high", "high"))
        self.assertEqual(notification.snapshot_id, latest_daily.snapshot_id)

    def test_fetch_uses_and_advances_the_persisted_cursor(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        self.records.set_cursor("athlete-001", "watch", "cursor-before", self.clock())
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "cursor-record",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
            next_cursor="cursor-after",
        )

        result = self.loop({"watch": connector}).run_once(limit=1)

        self.assertEqual((result.completed, result.failed), (1, 0))
        self.assertEqual(connector.cursors, ["cursor-before"])
        self.assertEqual(
            self.records.get_cursor("athlete-001", "watch"),
            "cursor-after",
        )

    def test_transient_fetch_failure_retries_before_ingestion(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        connector = FailsOnceConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "retry-success",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
        )

        result = self.loop({"watch": connector}).run_once(limit=1)

        run = self.automation.latest_sync_run("athlete-001", "watch")
        assert run is not None
        self.assertEqual((result.completed, result.failed), (1, 0))
        self.assertEqual(run.status, "completed")
        self.assertEqual(connector.attempts, 2)
        self.assertEqual(len(self.records.list_current("athlete-001")), 1)

    def test_repeated_lease_checks_only_renew_near_expiry(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        claimed = self.automation.claim_due(
            self.clock(),
            owner="integration-worker",
            lease_duration=timedelta(minutes=5),
            limit=1,
        )[0][1]
        assert claimed.lease_expires_at is not None
        original_expiry = claimed.lease_expires_at
        loop = self.loop({})

        current = claimed
        for _ in range(10):
            current = loop._renew_owned(current)
        self.assertEqual(current.lease_expires_at, original_expiry)

        self.clock.now += timedelta(minutes=4)
        current = loop._renew_owned(current)
        self.assertEqual(current.lease_expires_at, self.clock() + timedelta(minutes=5))
        renewed_expiry = current.lease_expires_at

        for _ in range(10):
            current = loop._renew_owned(current)
        self.assertEqual(current.lease_expires_at, renewed_expiry)

    def test_fenced_ingestion_blocks_reclaim_and_rejects_stale_token(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        claimed = self.automation.claim_due(
            self.clock(),
            owner="integration-worker",
            lease_duration=timedelta(minutes=5),
            limit=1,
        )[0][1]
        assert claimed.lease_token is not None
        transaction = getattr(self.automation, "lease_fenced_transaction", None)
        self.assertIsNotNone(transaction)
        assert transaction is not None
        ingestion = IngestionService(
            athlete_id="athlete-001",
            store=self.records,
            raw_dir=self.root / "raw" / "athlete-001",
        )
        batch = SyncBatch(
            (
                _record(
                    "watch",
                    "check-in",
                    "fenced",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
            next_cursor="cursor-fenced",
        )
        started = Event()
        finished = Event()
        reclaimed_runs = []

        def reclaim() -> None:
            started.set()
            reclaimed_runs.append(
                self.automation.reclaim_sync_run(
                    claimed.run_id,
                    now=self.clock(),
                    owner="takeover-worker",
                    lease_duration=timedelta(minutes=5),
                )
            )
            finished.set()

        staging_root = self.root / "raw" / "athlete-001" / ".test-staging"
        self.clock.now += timedelta(minutes=6)
        with transaction(
            claimed.run_id,
            lease_token=claimed.lease_token,
            owner="integration-worker",
            now=claimed.started_at,
        ) as connection:
            thread = Thread(target=reclaim, daemon=True)
            thread.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(finished.wait(0.05))
            result = ingestion.ingest(
                batch,
                connection=connection,
                raw_staging_dir=staging_root,
            )
            change_set = self.automation.save_change_set(
                claimed.run_id,
                result,
                created_at=claimed.started_at,
                lease_token=claimed.lease_token,
                connection=connection,
            )

        self.assertTrue(finished.wait(2))
        thread.join(timeout=2)
        self.assertEqual(len(reclaimed_runs), 1)
        self.assertEqual(reclaimed_runs[0].lease_owner, "takeover-worker")
        self.assertEqual(change_set.run_id, claimed.run_id)
        self.assertEqual(len(self.records.list_current("athlete-001")), 1)
        self.assertEqual(self.records.get_cursor("athlete-001", "watch"), "cursor-fenced")
        with (
            self.assertRaisesRegex(ValueError, "lease"),
            transaction(
                claimed.run_id,
                lease_token=claimed.lease_token,
                owner="integration-worker",
                now=self.clock(),
            ),
        ):
            self.fail("stale lease unexpectedly entered fenced transaction")

    def test_failed_fenced_ingestion_rolls_back_records_cursor_change_set_and_raw(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        first = _record(
            "watch",
            "check-in",
            "valid-before-failure",
            occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
            pain=0,
        )
        invalid = RawSourceRecord(
            source="watch",
            kind="check-in",
            source_id="invalid",
            occurred_at=datetime(2026, 10, 26, 9, 0, tzinfo=UTC),
            payload={"unsupported": object()},  # type: ignore[dict-item]
        )
        connector = MutableConnector("watch", (first, invalid), next_cursor="must-rollback")

        result = self.loop({"watch": connector}).run_once(limit=1)

        run = self.automation.latest_sync_run("athlete-001", "watch")
        assert run is not None
        self.assertEqual((result.completed, result.failed), (0, 1))
        self.assertEqual(run.status, "failed")
        self.assertEqual(self.records.list_current("athlete-001"), [])
        self.assertIsNone(self.records.get_cursor("athlete-001", "watch"))
        self.assertEqual(self.table_count("data_change_sets"), 0)
        raw_root = self.root / "raw" / "athlete-001"
        self.assertFalse(raw_root.exists() and any(raw_root.rglob("*.json")))

    def test_raw_promotion_failure_rolls_back_the_fenced_database_transaction(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "promotion-fails",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
            next_cursor="must-rollback",
        )

        with patch.object(
            IngestionService,
            "promote_raw_files",
            side_effect=OSError("simulated raw promotion failure"),
        ):
            result = self.loop({"watch": connector}).run_once(limit=1)

        run = self.automation.latest_sync_run("athlete-001", "watch")
        assert run is not None
        self.assertEqual((result.completed, result.failed), (0, 1))
        self.assertEqual(run.status, "failed")
        self.assertEqual(self.records.list_current("athlete-001"), [])
        self.assertIsNone(self.records.get_cursor("athlete-001", "watch"))
        self.assertEqual(self.table_count("data_change_sets"), 0)

    def test_raw_evidence_is_promoted_before_the_database_commit(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "promotion-before-commit",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
        )
        original_promote = IngestionService.promote_raw_files
        committed_change_sets_seen: list[int] = []

        def observe_promotion(ingestion_result) -> None:
            with closing(sqlite3.connect(self.database_file)) as connection:
                row = connection.execute("SELECT COUNT(*) FROM data_change_sets").fetchone()
            assert row is not None
            committed_change_sets_seen.append(int(row[0]))
            original_promote(ingestion_result)
            self.assertTrue(
                all(destination.is_file() for _, destination in ingestion_result.staged_raw_files)
            )

        with patch.object(
            IngestionService,
            "promote_raw_files",
            side_effect=observe_promotion,
        ):
            result = self.loop({"watch": connector}).run_once(limit=1)

        self.assertEqual((result.completed, result.failed), (1, 0))
        self.assertEqual(committed_change_sets_seen, [0])

    def test_raw_staging_directory_is_hidden_beneath_athlete_raw_root(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "same-filesystem-staging",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
        )
        original_stage = IngestionService._stage_raw_file
        staging_directories: list[Path] = []

        def observe_staging(destination, payload, source, digest, *, raw_root, raw_staging_dir):
            assert raw_staging_dir is not None
            staging_directories.append(raw_staging_dir)
            return original_stage(
                destination,
                payload,
                source,
                digest,
                raw_root=raw_root,
                raw_staging_dir=raw_staging_dir,
            )

        with patch.object(
            IngestionService,
            "_stage_raw_file",
            side_effect=observe_staging,
        ):
            result = self.loop({"watch": connector}).run_once(limit=1)

        athlete_raw_root = self.root / "raw" / "athlete-001"
        self.assertEqual((result.completed, result.failed), (1, 0))
        self.assertTrue(staging_directories)
        self.assertTrue(
            all(
                directory.parent.resolve() == athlete_raw_root.resolve()
                for directory in staging_directories
            )
        )
        self.assertTrue(
            all(directory.name.startswith(".staging-") for directory in staging_directories)
        )
        self.assertFalse(
            any(path.name.startswith(".staging-") for path in athlete_raw_root.iterdir())
        )

    def test_lease_expiry_during_raw_promotion_rolls_back_database_work(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "expires-during-promotion",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
            next_cursor="must-rollback",
        )
        original_promote = IngestionService.promote_raw_files

        def promote_then_expire(ingestion_result) -> None:
            original_promote(ingestion_result)
            self.clock.now += timedelta(minutes=6)

        with patch.object(
            IngestionService,
            "promote_raw_files",
            side_effect=promote_then_expire,
        ):
            result = self.loop({"watch": connector}).run_once(limit=1)

        run = self.automation.latest_sync_run("athlete-001", "watch")
        assert run is not None
        self.assertEqual((result.completed, result.failed), (0, 1))
        self.assertEqual(run.status, "running")
        self.assertIn(
            run.run_id,
            {item.run_id for item in self.automation.list_recoverable_runs(self.clock())},
        )
        self.assertEqual(self.records.list_current("athlete-001"), [])
        self.assertIsNone(self.records.get_cursor("athlete-001", "watch"))
        self.assertEqual(self.table_count("data_change_sets"), 0)

    def test_recovery_refuses_a_change_set_with_missing_raw_evidence(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "raw-removed-before-recovery",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
        )
        loop = self.loop({"watch": connector})

        with (
            patch.object(
                self.automation,
                "save_snapshot",
                side_effect=SystemExit("simulated crash after change set"),
            ),
            self.assertRaisesRegex(SystemExit, "after change set"),
        ):
            loop.run_once(limit=1)

        crashed_run = self.automation.latest_sync_run("athlete-001", "watch")
        assert crashed_run is not None
        raw_files = list((self.root / "raw" / "athlete-001").rglob("*.json"))
        self.assertTrue(raw_files)
        for raw_file in raw_files:
            raw_file.unlink()
        self.clock.now += timedelta(minutes=6)

        result = loop.run_once(limit=1)

        recovered = self.automation.get_sync_run(crashed_run.run_id)
        assert recovered is not None
        self.assertEqual((result.recovered, result.completed, result.failed), (1, 0, 1))
        self.assertEqual(recovered.status, "failed")
        self.assertEqual(self.automation.list_snapshots("athlete-001"), [])

    def test_recovery_refuses_safe_raw_file_with_wrong_content(self) -> None:
        loop, run_id, raw_file = self.crash_after_raw_change_set("tampered-in-vault")
        raw_file.write_bytes(b"tampered")
        self.clock.now += timedelta(minutes=6)

        result = loop.run_once(limit=1)

        recovered = self.automation.get_sync_run(run_id)
        assert recovered is not None
        self.assertEqual((result.recovered, result.completed, result.failed), (1, 0, 1))
        self.assertEqual(recovered.status, "failed")
        self.assertEqual(self.automation.list_snapshots("athlete-001"), [])

    def test_recovery_refuses_symlinked_raw_source_escape(self) -> None:
        loop, run_id, raw_file = self.crash_after_raw_change_set("symlinked-source")
        athlete_raw_root = self.root / "raw" / "athlete-001"
        source_root = athlete_raw_root / "watch"
        relative_raw = raw_file.relative_to(source_root)
        outside_source = self.root / "outside-source"
        outside_file = outside_source / relative_raw
        outside_file.parent.mkdir(parents=True)
        outside_file.write_bytes(b"mismatched outside content")
        shutil.rmtree(source_root)
        source_root.symlink_to(outside_source, target_is_directory=True)
        self.clock.now += timedelta(minutes=6)

        result = loop.run_once(limit=1)

        recovered = self.automation.get_sync_run(run_id)
        assert recovered is not None
        self.assertEqual((result.recovered, result.completed, result.failed), (1, 0, 1))
        self.assertEqual(recovered.status, "failed")
        self.assertEqual(self.automation.list_snapshots("athlete-001"), [])

    def test_recovery_accepts_valid_content_addressed_raw_evidence(self) -> None:
        loop, run_id, _ = self.crash_after_raw_change_set("valid-evidence")
        self.clock.now += timedelta(minutes=6)

        result = loop.run_once(limit=1)

        recovered = self.automation.get_sync_run(run_id)
        assert recovered is not None
        self.assertEqual((result.recovered, result.completed, result.failed), (1, 1, 0))
        self.assertEqual(recovered.status, "completed")
        self.assertEqual(len(self.automation.list_snapshots("athlete-001")), 2)
        self.assertEqual(len(self.automation.list_unread("athlete-001")), 1)

    def test_reclaim_during_fetch_prevents_stale_worker_mutation(self) -> None:
        self.add_profile()
        self.add_subscription("watch")

        def reclaim_during_fetch() -> None:
            stale = self.automation.latest_sync_run("athlete-001", "watch")
            assert stale is not None
            self.clock.now += timedelta(minutes=11)
            self.automation.reclaim_sync_run(
                stale.run_id,
                now=self.clock(),
                owner="takeover-worker",
                lease_duration=timedelta(minutes=5),
            )

        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "must-not-persist",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
            after_fetch=reclaim_during_fetch,
        )

        result = self.loop({"watch": connector}).run_once(limit=1)

        reclaimed = self.automation.latest_sync_run("athlete-001", "watch")
        assert reclaimed is not None
        self.assertEqual((result.claimed, result.completed, result.failed), (1, 0, 1))
        self.assertEqual(reclaimed.status, "running")
        self.assertEqual(reclaimed.lease_owner, "takeover-worker")
        self.assertEqual(self.records.list_current("athlete-001"), [])
        self.assertEqual(self.table_count("data_change_sets"), 0)
        self.assertFalse((self.root / "raw" / "athlete-001").exists())

    def test_reclaim_immediately_before_snapshot_fences_the_stale_effect_write(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "snapshot-fence",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
        )
        original_save = self.automation.save_snapshot

        def reclaim_then_save(*args, **kwargs):
            stale = self.automation.latest_sync_run("athlete-001", "watch")
            assert stale is not None
            self.clock.now += timedelta(minutes=6)
            self.automation.reclaim_sync_run(
                stale.run_id,
                now=self.clock(),
                owner="snapshot-takeover",
                lease_duration=timedelta(minutes=5),
            )
            return original_save(*args, **kwargs)

        with patch.object(
            self.automation,
            "save_snapshot",
            side_effect=reclaim_then_save,
        ):
            result = self.loop({"watch": connector}).run_once(limit=1)

        current = self.automation.latest_sync_run("athlete-001", "watch")
        assert current is not None
        self.assertEqual((result.completed, result.failed), (0, 1))
        self.assertEqual((current.status, current.lease_owner), ("running", "snapshot-takeover"))
        self.assertEqual(self.automation.list_snapshots("athlete-001"), [])

    def test_lease_expiry_before_snapshot_transaction_blocks_first_snapshot(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "expires-before-snapshot",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
        )
        original_save = self.automation.save_snapshot

        def expire_then_save(*args, **kwargs):
            self.clock.now += timedelta(minutes=6)
            return original_save(*args, **kwargs)

        with patch.object(
            self.automation,
            "save_snapshot",
            side_effect=expire_then_save,
        ):
            result = self.loop({"watch": connector}).run_once(limit=1)

        run = self.automation.latest_sync_run("athlete-001", "watch")
        assert run is not None
        self.assertEqual((result.completed, result.failed), (0, 1))
        self.assertEqual(run.status, "running")
        self.assertIn(
            run.run_id,
            {item.run_id for item in self.automation.list_recoverable_runs(self.clock())},
        )
        self.assertEqual(self.automation.list_snapshots("athlete-001"), [])
        self.assertEqual(self.automation.list_unread("athlete-001"), [])

    def test_reclaim_immediately_before_notification_fences_the_stale_effect_write(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "notification-fence",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
        )
        original_enqueue = self.automation.enqueue_notification

        def reclaim_then_enqueue(*args, **kwargs):
            stale = self.automation.latest_sync_run("athlete-001", "watch")
            assert stale is not None
            self.clock.now += timedelta(minutes=6)
            self.automation.reclaim_sync_run(
                stale.run_id,
                now=self.clock(),
                owner="notification-takeover",
                lease_duration=timedelta(minutes=5),
            )
            return original_enqueue(*args, **kwargs)

        with patch.object(
            self.automation,
            "enqueue_notification",
            side_effect=reclaim_then_enqueue,
        ):
            result = self.loop({"watch": connector}).run_once(limit=1)

        current = self.automation.latest_sync_run("athlete-001", "watch")
        assert current is not None
        self.assertEqual((result.completed, result.failed), (0, 1))
        self.assertEqual(
            (current.status, current.lease_owner),
            ("running", "notification-takeover"),
        )
        self.assertEqual(self.automation.list_unread("athlete-001"), [])

    def test_lease_expiry_before_notification_transaction_blocks_notification(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "expires-before-notification",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
        )
        original_enqueue = self.automation.enqueue_notification

        def expire_then_enqueue(*args, **kwargs):
            self.clock.now += timedelta(minutes=6)
            return original_enqueue(*args, **kwargs)

        with patch.object(
            self.automation,
            "enqueue_notification",
            side_effect=expire_then_enqueue,
        ):
            result = self.loop({"watch": connector}).run_once(limit=1)

        run = self.automation.latest_sync_run("athlete-001", "watch")
        assert run is not None
        self.assertEqual((result.completed, result.failed), (0, 1))
        self.assertEqual(run.status, "running")
        self.assertIn(
            run.run_id,
            {item.run_id for item in self.automation.list_recoverable_runs(self.clock())},
        )
        self.assertEqual(len(self.automation.list_snapshots("athlete-001")), 2)
        self.assertEqual(self.automation.list_unread("athlete-001"), [])

    def test_lease_expiry_before_terminal_transaction_prevents_completion(self) -> None:
        self.add_profile()
        self.add_subscription("watch", mode=NotificationMode.OFF)
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "expires-before-completion",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
        )
        original_complete = self.automation.complete_sync_run

        def expire_then_complete(*args, **kwargs):
            self.clock.now += timedelta(minutes=6)
            return original_complete(*args, **kwargs)

        with patch.object(
            self.automation,
            "complete_sync_run",
            side_effect=expire_then_complete,
        ):
            result = self.loop({"watch": connector}).run_once(limit=1)

        run = self.automation.latest_sync_run("athlete-001", "watch")
        assert run is not None
        self.assertEqual((result.completed, result.failed), (0, 1))
        self.assertEqual(run.status, "running")
        self.assertIn(
            run.run_id,
            {item.run_id for item in self.automation.list_recoverable_runs(self.clock())},
        )
        self.assertEqual(len(self.automation.list_snapshots("athlete-001")), 2)
        self.assertEqual(self.automation.list_unread("athlete-001"), [])

    def test_each_due_run_is_claimed_just_in_time(self) -> None:
        self.add_profile()
        self.add_subscription("a-slow")
        self.add_subscription("b-fast")
        initial_now = self.clock.now

        def advance_during_first_fetch() -> None:
            self.clock.now += timedelta(minutes=6)

        first = MutableConnector(
            "a-slow",
            (
                _record(
                    "a-slow",
                    "check-in",
                    "first",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
            after_fetch=advance_during_first_fetch,
        )
        second = MutableConnector(
            "b-fast",
            (
                _record(
                    "b-fast",
                    "check-in",
                    "second",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
        )

        result = self.loop({"a-slow": first, "b-fast": second}).run_once(limit=2)

        first_run = self.automation.latest_sync_run("athlete-001", "a-slow")
        second_run = self.automation.latest_sync_run("athlete-001", "b-fast")
        assert first_run is not None and second_run is not None
        self.assertEqual((result.claimed, result.completed, result.failed), (2, 1, 1))
        self.assertEqual(first_run.started_at, initial_now)
        self.assertEqual(first_run.status, "running")
        assert first_run.lease_expires_at is not None
        self.assertLessEqual(first_run.lease_expires_at, self.clock())
        self.assertEqual(second_run.started_at, initial_now + timedelta(minutes=6))
        self.assertEqual(second_run.status, "completed")
        self.assertEqual(len(self.records.list_current("athlete-001")), 1)

    def test_digest_is_due_at_the_exact_configured_local_hour(self) -> None:
        boundary = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)

        self.assertEqual(
            AutomaticTrainingLoop._next_digest(boundary, "Asia/Shanghai", 23),
            boundary,
        )

    def test_digest_uses_next_valid_instant_for_spring_forward_gap(self) -> None:
        before_gap = datetime(2026, 3, 8, 6, 30, tzinfo=UTC)

        self.assertEqual(
            AutomaticTrainingLoop._next_digest(before_gap, "America/New_York", 2),
            datetime(2026, 3, 8, 7, 0, tzinfo=UTC),
        )

    def test_digest_can_use_second_fall_back_occurrence(self) -> None:
        between_occurrences = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)

        self.assertEqual(
            AutomaticTrainingLoop._next_digest(
                between_occurrences,
                "America/New_York",
                1,
            ),
            datetime(2026, 11, 1, 6, 0, tzinfo=UTC),
        )

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
            projection_record_ids=self.records.list_current_ids("athlete-001"),
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

    def test_recovery_fails_safely_when_legacy_projection_is_unavailable(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "legacy-without-watermark",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                ),
            ),
        )
        crashed_run = self.automation.claim_due(
            self.clock(),
            owner="legacy-worker",
            lease_duration=timedelta(minutes=1),
        )[0][1]
        assert crashed_run.lease_token is not None
        ingestion_result = IngestionService(
            athlete_id="athlete-001",
            store=self.records,
            raw_dir=self.root / "raw" / "athlete-001",
        ).sync(connector, SyncWindow(date(2026, 10, 24), date(2026, 10, 26)))
        self.automation.save_change_set(
            crashed_run.run_id,
            ingestion_result,
            created_at=self.clock(),
            lease_token=crashed_run.lease_token,
        )
        self.clock.now += timedelta(minutes=2)

        result = self.loop({}).run_once(limit=1)

        failed_run = self.automation.get_sync_run(crashed_run.run_id)
        assert failed_run is not None
        self.assertEqual((result.recovered, result.completed, result.failed), (1, 0, 1))
        self.assertEqual(failed_run.status, "failed")
        self.assertEqual(self.automation.list_snapshots("athlete-001"), [])

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

    def test_recovery_treats_partial_snapshots_as_authoritative(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "partial-low-older",
                    occurred_at=datetime(2026, 10, 25, 8, 0, tzinfo=UTC),
                    pain=0,
                    fatigue=2,
                    soreness=2,
                    illness=False,
                ),
                _record(
                    "watch",
                    "check-in",
                    "partial-low-newer",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                    fatigue=2,
                    soreness=2,
                    illness=False,
                ),
            ),
        )
        loop = self.loop({"watch": connector})
        original_save = self.automation.save_snapshot
        save_calls = 0

        def crash_before_second_snapshot(*args, **kwargs):
            nonlocal save_calls
            save_calls += 1
            if save_calls == 2:
                raise SystemExit("simulated crash between snapshots")
            return original_save(*args, **kwargs)

        with (
            patch.object(
                self.automation,
                "save_snapshot",
                side_effect=crash_before_second_snapshot,
            ),
            self.assertRaisesRegex(SystemExit, "between snapshots"),
        ):
            loop.run_once(limit=1)

        crashed_run = self.automation.latest_sync_run("athlete-001", "watch")
        assert crashed_run is not None
        change_set = self.automation.get_change_set_for_run(crashed_run.run_id)
        assert change_set is not None
        original = self.automation.list_snapshots_for_change_set(change_set.change_set_id)
        self.assertEqual(len(original), 1)
        self.assertEqual(
            self.automation.list_notifications_for_change_set(change_set.change_set_id), []
        )
        original_snapshot = original[0]
        self.add_subscription("other-source")
        later_high = MutableConnector(
            "other-source",
            (
                _record(
                    "other-source",
                    "check-in",
                    "later-high-on-missing-day",
                    occurred_at=datetime(2026, 10, 26, 10, 0, tzinfo=UTC),
                    pain=8,
                    fatigue=2,
                    soreness=2,
                    illness=False,
                ),
            ),
        )
        later_result = self.loop({"other-source": later_high}).run_once(limit=1)
        later_run = self.automation.latest_sync_run("athlete-001", "other-source")
        assert later_run is not None
        later_change_set = self.automation.get_change_set_for_run(later_run.run_id)
        assert later_change_set is not None
        later_snapshots = self.automation.list_snapshots_for_change_set(
            later_change_set.change_set_id
        )
        self.assertEqual((later_result.completed, later_result.failed), (1, 0))
        self.assertIn("high", {item.risk_level for item in later_snapshots})
        self.clock.now += timedelta(minutes=6)

        result = loop.run_once(limit=1)

        recovered = self.automation.get_sync_run(crashed_run.run_id)
        snapshots = self.automation.list_snapshots_for_change_set(change_set.change_set_id)
        notifications = self.automation.list_notifications_for_change_set(change_set.change_set_id)
        assert recovered is not None
        self.assertEqual((result.recovered, result.completed, result.failed), (1, 1, 0))
        self.assertEqual(recovered.status, "completed")
        self.assertEqual(len(snapshots), 3)
        self.assertIn(original_snapshot, snapshots)
        self.assertEqual(
            {item.period for item in snapshots if item.analysis_kind == "daily"},
            {"2026-10-25", "2026-10-26"},
        )
        self.assertEqual(sum(item.analysis_kind == "weekly" for item in snapshots), 1)
        self.assertEqual(len(notifications), 1)
        self.assertIn(notifications[0].snapshot_id, {item.snapshot_id for item in snapshots})
        self.assertEqual({item.risk_level for item in snapshots}, {"low"})
        self.assertEqual(notifications[0].priority, "digest")
        self.assertNotIn(
            notifications[0].snapshot_id,
            {item.snapshot_id for item in later_snapshots},
        )

    def test_recovery_reuses_snapshots_after_other_source_changes_same_day(self) -> None:
        self.add_profile()
        self.add_subscription("watch")
        low_connector = MutableConnector(
            "watch",
            (
                _record(
                    "watch",
                    "check-in",
                    "original-low",
                    occurred_at=datetime(2026, 10, 26, 8, 0, tzinfo=UTC),
                    pain=0,
                    fatigue=2,
                    soreness=2,
                    illness=False,
                ),
            ),
        )
        loop = self.loop({"watch": low_connector})

        with (
            patch.object(
                self.automation,
                "complete_sync_run",
                side_effect=SystemExit("simulated crash after persisted effects"),
            ),
            self.assertRaisesRegex(SystemExit, "persisted effects"),
        ):
            loop.run_once(limit=1)

        crashed_run = self.automation.latest_sync_run("athlete-001", "watch")
        assert crashed_run is not None
        original_snapshots = self.automation.list_snapshots("athlete-001")
        original_notification = self.automation.list_unread("athlete-001")[0]
        original_daily = next(item for item in original_snapshots if item.analysis_kind == "daily")
        self.assertEqual(original_daily.risk_level, "low")

        IngestionService(
            athlete_id="athlete-001",
            store=self.records,
            raw_dir=self.root / "raw" / "athlete-001",
        ).ingest(
            SyncBatch(
                (
                    _record(
                        "other-source",
                        "check-in",
                        "later-high",
                        occurred_at=datetime(2026, 10, 26, 10, 0, tzinfo=UTC),
                        pain=8,
                        fatigue=2,
                        soreness=2,
                        illness=False,
                    ),
                )
            )
        )
        self.clock.now += timedelta(days=1)

        result = loop.run_once(limit=1)

        recovered = self.automation.get_sync_run(crashed_run.run_id)
        snapshots = self.automation.list_snapshots("athlete-001")
        notifications = self.automation.list_unread("athlete-001")
        assert recovered is not None
        self.assertEqual((result.recovered, result.completed, result.failed), (1, 1, 0))
        self.assertEqual(recovered.status, "completed")
        self.assertEqual(snapshots, original_snapshots)
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].notification_id, original_notification.notification_id)
        self.assertEqual(notifications[0].snapshot_id, original_daily.snapshot_id)


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
