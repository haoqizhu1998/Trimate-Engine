from __future__ import annotations

import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock
from unittest.mock import patch

from athlete_growth.adapters.storage import (
    ProfileRegistry,
    SQLiteAutomationRepository,
    VaultLayout,
)
from athlete_growth.application.automation import NotificationMode
from athlete_growth.interfaces.cli import main as cli_main
from athlete_growth.interfaces.host import tools as host_tools
from athlete_growth.ports.data_source import (
    HealthStatus,
    SourceCapabilities,
    SyncBatch,
    SyncWindow,
)
from athlete_growth.ports.notification import NoopNotificationSink


class EmptyGarminConnector:
    source = "garmin-cn"

    def __init__(self, *, home: Path) -> None:
        self.home = home
        self.windows: list[SyncWindow] = []

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities("garmin-cn", "cn", ("activity",))

    def sync(self, window: SyncWindow, cursor: str | None) -> SyncBatch:
        del cursor
        self.windows.append(window)
        return SyncBatch((), None)

    def healthcheck(self) -> HealthStatus:
        return HealthStatus(True, "unused")

    def revoke(self) -> None:
        return None


class BlockingGarminConnector(EmptyGarminConnector):
    started = Event()
    release = Event()
    calls = 0
    calls_lock = Lock()

    def sync(self, window: SyncWindow, cursor: str | None) -> SyncBatch:
        with self.calls_lock:
            type(self).calls += 1
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release connector")
        return super().sync(window, cursor)


class FailingGarminConnector(EmptyGarminConnector):
    def sync(self, window: SyncWindow, cursor: str | None) -> SyncBatch:
        del window, cursor
        raise ConnectionError("vendor included a secret in the original error")


class AutomationHostAndCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.home = Path(self.temporary_directory.name) / "data-home"
        with redirect_stdout(StringIO()):
            code = cli_main.main(
                [
                    "init",
                    "--name",
                    "Automation Athlete",
                    "--timezone",
                    "Asia/Shanghai",
                    "--data-home",
                    str(self.home),
                ]
            )
        self.assertEqual(code, 0)
        athlete_id = ProfileRegistry(self.home / "config.toml").default_profile_id()
        assert athlete_id is not None
        self.athlete_id = athlete_id
        self.layout = VaultLayout(self.home, athlete_id)
        self.repository = SQLiteAutomationRepository(self.layout.database_file)
        self.now = datetime(2026, 10, 26, 12, 34, 56, tzinfo=UTC)

    def test_configure_defaults_and_overrides_persist(self) -> None:
        configured = host_tools.configure_automation(self.home, now=self.now)

        self.assertEqual(
            configured["policy"],
            {
                "source": "garmin-cn",
                "interval_minutes": 30,
                "lookback_days": 3,
                "notification_mode": "actionable-digest",
                "digest_hour_local": 20,
                "enabled": True,
            },
        )
        self.assertEqual(configured["next_run_at"], self.now.isoformat())
        stored = self.repository.get_subscription(self.athlete_id, "garmin-cn")
        assert stored is not None
        self.assertEqual(stored.notification_mode, NotificationMode.ACTIONABLE_DIGEST)
        self.assertEqual(self.repository.get_next_run_at(self.athlete_id, "garmin-cn"), self.now)

        overridden = host_tools.configure_automation(
            self.home,
            interval_minutes=45,
            lookback_days=7,
            notification_mode=NotificationMode.ALL,
            digest_hour_local=8,
            enabled=False,
            now=self.now,
        )
        self.assertEqual(overridden["policy"]["interval_minutes"], 45)
        self.assertEqual(overridden["policy"]["lookback_days"], 7)
        self.assertEqual(overridden["policy"]["notification_mode"], "all")
        self.assertEqual(overridden["policy"]["digest_hour_local"], 8)
        self.assertFalse(overridden["policy"]["enabled"])

    def test_status_is_safe_and_reports_run_analysis_and_backlogs(self) -> None:
        host_tools.configure_automation(self.home, now=self.now)
        subscription = self.repository.get_subscription(self.athlete_id, "garmin-cn")
        assert subscription is not None
        run = self.repository.start_sync_run(
            subscription,
            now=self.now,
            reason="manual",
        )
        self.repository.fail_sync_run(
            run.run_id,
            error="ConnectionError: automatic sync failed",
            completed_at=self.now,
        )

        status = host_tools.automation_status(self.home, now=self.now)
        encoded = json.dumps(status)

        self.assertEqual(status["policies"][0]["last_run"]["status"], "failed")
        self.assertEqual(status["policies"][0]["last_run"]["error_class"], "ConnectionError")
        self.assertEqual(status["backlog"]["due_subscriptions"], 1)
        self.assertIsNone(status["last_analysis"])
        for secret in (
            "athlete.sqlite3",
            "lease_token",
            "session",
            "healthcheck",
            "automatic sync failed",
        ):
            self.assertNotIn(secret, encoded)

    def test_status_maps_unknown_error_prefix_to_fixed_safe_value(self) -> None:
        host_tools.configure_automation(self.home, now=self.now)
        subscription = self.repository.get_subscription(self.athlete_id, "garmin-cn")
        assert subscription is not None
        run = self.repository.start_sync_run(
            subscription,
            now=self.now,
            reason="manual",
        )
        self.repository.fail_sync_run(
            run.run_id,
            error="SecretAccessKey: automatic sync failed",
            completed_at=self.now,
        )

        status = host_tools.automation_status(self.home, now=self.now)
        encoded = json.dumps(status)

        self.assertEqual(status["policies"][0]["last_run"]["error_class"], "UnknownError")
        self.assertNotIn("SecretAccessKey", encoded)

    @patch(
        "athlete_growth.interfaces.host.tools.default_notification_sink",
        return_value=NoopNotificationSink(),
    )
    @patch(
        "athlete_growth.interfaces.host.tools.GarminCnConnector",
        EmptyGarminConnector,
    )
    def test_force_run_is_minute_idempotent(self, _sink) -> None:
        future_schedule = datetime(2026, 10, 27, tzinfo=UTC)
        host_tools.configure_automation(
            self.home,
            next_run_at=future_schedule,
        )

        first = host_tools.run_automation_once(
            self.home,
            force=True,
            clock=lambda: self.now,
        )
        second = host_tools.run_automation_once(
            self.home,
            force=True,
            clock=lambda: self.now,
        )

        self.assertEqual(first["claimed"], 1)
        self.assertEqual(first["completed"], 1)
        self.assertEqual(second["claimed"], 0)
        latest = self.repository.latest_sync_run(self.athlete_id, "garmin-cn")
        assert latest is not None
        self.assertEqual(latest.status, "completed")
        self.assertEqual(latest.reason, "force")
        self.assertEqual(
            self.repository.get_next_run_at(self.athlete_id, "garmin-cn"),
            future_schedule,
        )

    @patch(
        "athlete_growth.interfaces.host.tools.default_notification_sink",
        return_value=NoopNotificationSink(),
    )
    @patch(
        "athlete_growth.interfaces.host.tools.GarminCnConnector",
        EmptyGarminConnector,
    )
    def test_force_is_distinct_from_completed_schedule_in_same_minute(self, _sink) -> None:
        slot = self.now.replace(second=0, microsecond=0)
        host_tools.configure_automation(self.home, next_run_at=slot)

        scheduled = host_tools.run_automation_once(self.home, clock=lambda: self.now)
        forced = host_tools.run_automation_once(
            self.home,
            force=True,
            clock=lambda: self.now,
        )

        self.assertEqual(scheduled["completed"], 1)
        self.assertEqual(forced["completed"], 1)
        with sqlite3.connect(self.layout.database_file) as connection:
            rows = connection.execute(
                """
                SELECT reason, request_key FROM automation_sync_runs
                WHERE athlete_id = ? AND source = 'garmin-cn'
                ORDER BY reason
                """,
                (self.athlete_id,),
            ).fetchall()
        self.assertEqual([row[0] for row in rows], ["force", "schedule"])
        self.assertEqual(len({row[1] for row in rows}), 2)

    @patch(
        "athlete_growth.interfaces.host.tools.default_notification_sink",
        return_value=NoopNotificationSink(),
    )
    def test_force_is_distinct_from_failed_schedule_in_same_minute(self, _sink) -> None:
        slot = self.now.replace(second=0, microsecond=0)
        host_tools.configure_automation(self.home, next_run_at=slot)
        with patch(
            "athlete_growth.interfaces.host.tools.GarminCnConnector",
            FailingGarminConnector,
        ):
            scheduled = host_tools.run_automation_once(self.home, clock=lambda: self.now)
        with patch(
            "athlete_growth.interfaces.host.tools.GarminCnConnector",
            EmptyGarminConnector,
        ):
            forced = host_tools.run_automation_once(
                self.home,
                force=True,
                clock=lambda: self.now,
            )

        self.assertEqual(scheduled["failed"], 1)
        self.assertEqual(forced["completed"], 1)
        with sqlite3.connect(self.layout.database_file) as connection:
            rows = connection.execute(
                """
                SELECT reason, status FROM automation_sync_runs
                WHERE athlete_id = ? AND source = 'garmin-cn'
                ORDER BY reason
                """,
                (self.athlete_id,),
            ).fetchall()
        self.assertEqual(rows, [("force", "completed"), ("schedule", "failed")])

    @patch(
        "athlete_growth.interfaces.host.tools.default_notification_sink",
        return_value=NoopNotificationSink(),
    )
    @patch(
        "athlete_growth.interfaces.host.tools.GarminCnConnector",
        EmptyGarminConnector,
    )
    def test_run_once_processes_due_work_without_force(self, _sink) -> None:
        host_tools.configure_automation(self.home, now=self.now)

        summary = host_tools.run_automation_once(
            self.home,
            clock=lambda: self.now,
        )

        self.assertEqual(summary["claimed"], 1)
        self.assertEqual(summary["completed"], 1)

    @patch(
        "athlete_growth.interfaces.host.tools.default_notification_sink",
        return_value=NoopNotificationSink(),
    )
    @patch(
        "athlete_growth.interfaces.host.tools.GarminCnConnector",
        BlockingGarminConnector,
    )
    def test_overlapping_force_runs_do_not_duplicate_work(self, _sink) -> None:
        BlockingGarminConnector.started.clear()
        BlockingGarminConnector.release.clear()
        BlockingGarminConnector.calls = 0
        due_slot = self.now.replace(second=0, microsecond=0)
        host_tools.configure_automation(
            self.home,
            next_run_at=due_slot,
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(
                host_tools.run_automation_once,
                self.home,
                force=True,
                clock=lambda: self.now,
            )
            self.assertTrue(BlockingGarminConnector.started.wait(timeout=5))
            second = pool.submit(
                host_tools.run_automation_once,
                self.home,
                force=True,
                clock=lambda: self.now,
            ).result(timeout=5)
            BlockingGarminConnector.release.set()
            first = first_future.result(timeout=5)

        ordinary = host_tools.run_automation_once(
            self.home,
            clock=lambda: self.now,
        )

        self.assertEqual(sorted((first["claimed"], second["claimed"])), [0, 1])
        self.assertEqual(first["completed"] + second["completed"], 1)
        self.assertEqual(ordinary["claimed"], 0)
        self.assertEqual(BlockingGarminConnector.calls, 1)

    def test_notification_list_and_read_enforce_current_athlete_ownership(self) -> None:
        own_id = self._insert_notification("own", self.athlete_id)
        other_id = self._insert_notification("other", "other-athlete")

        listed = host_tools.list_notifications(self.home)
        self.assertEqual([item["notification_id"] for item in listed["notifications"]], [own_id])
        self.assertNotIn("athlete_id", listed["notifications"][0])
        self.assertNotIn("snapshot_id", listed["notifications"][0])

        read = host_tools.read_notification(self.home, own_id, now=self.now)
        self.assertEqual(read["notification"]["read_at"], self.now.isoformat())
        self.assertEqual(host_tools.list_notifications(self.home)["notifications"], [])
        self.assertEqual(
            len(host_tools.list_notifications(self.home, include_read=True)["notifications"]),
            1,
        )
        status = host_tools.automation_status(self.home, now=self.now)
        self.assertEqual(status["last_analysis"]["risk_level"], "low")
        self.assertEqual(status["last_analysis"]["period"], "2026-10-26")
        self.assertEqual(status["backlog"]["unread_notifications"], 0)
        self.assertEqual(status["backlog"]["delivery_backlog"], 1)
        for inaccessible in (other_id, "missing-notification"):
            with self.assertRaisesRegex(KeyError, "notification not found"):
                host_tools.read_notification(self.home, inaccessible, now=self.now)

    def test_parser_and_main_expose_automation_commands_as_json(self) -> None:
        parser = cli_main._build_parser()
        configured_args = parser.parse_args(
            [
                "automation-configure",
                "--interval-minutes",
                "60",
                "--lookback-days",
                "5",
                "--notification-mode",
                "off",
                "--digest-hour",
                "9",
                "--disable",
                "--data-home",
                str(self.home),
            ]
        )
        self.assertEqual(configured_args.command, "automation-configure")
        self.assertTrue(configured_args.disable)
        self.assertEqual(
            parser.parse_args(["automation-configure", "--digest-hour-local", "7"]).digest_hour,
            7,
        )
        self.assertTrue(
            parser.parse_args(
                ["automation-run-once", "--force", "--data-home", str(self.home)]
            ).force
        )
        self.assertTrue(
            parser.parse_args(
                ["notification-list", "--include-read", "--data-home", str(self.home)]
            ).include_read
        )
        for command in (
            "automation-status",
            "automation-install-macos",
            "automation-uninstall-macos",
        ):
            self.assertEqual(
                parser.parse_args([command, "--data-home", str(self.home)]).command,
                command,
            )

        output = StringIO()
        with redirect_stdout(output):
            code = cli_main.main(
                [
                    "automation-configure",
                    "--disable",
                    "--data-home",
                    str(self.home),
                ]
            )
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output.getvalue())["policy"]["enabled"])
        stored = self.repository.get_subscription(self.athlete_id, "garmin-cn")
        assert stored is not None
        self.assertFalse(stored.enabled)

        error = StringIO()
        with redirect_stderr(error):
            code = cli_main.main(["notification-read", "missing", "--data-home", str(self.home)])
        self.assertEqual(code, 1)
        self.assertIn("notification not found", error.getvalue())

    def test_host_facade_exposes_command_aligned_callable_names(self) -> None:
        for name in (
            "automation_configure",
            "automation_status",
            "automation_run_once",
            "automation_install_macos",
            "automation_uninstall_macos",
            "notification_list",
            "notification_read",
        ):
            self.assertTrue(callable(getattr(host_tools, name, None)), name)

    def test_non_darwin_scheduler_host_calls_do_not_touch_profile_database(self) -> None:
        for function_name in (
            "automation_install_macos",
            "automation_uninstall_macos",
        ):
            with self.subTest(function_name=function_name):
                home = self._initialize_profile_without_automation(function_name)
                athlete_id = ProfileRegistry(home / "config.toml").default_profile_id()
                assert athlete_id is not None
                database_file = VaultLayout(home, athlete_id).database_file
                before_bytes = database_file.read_bytes()
                before_tables = self._table_names(database_file)
                fake_user_home = home.parent / f"{function_name}-user-home"

                with (
                    patch("athlete_growth.adapters.scheduler.macos.sys.platform", "linux"),
                    patch(
                        "athlete_growth.adapters.scheduler.macos.Path.home",
                        return_value=fake_user_home,
                    ),
                    self.assertRaisesRegex(RuntimeError, "macOS"),
                ):
                    getattr(host_tools, function_name)(home)

                self.assertEqual(database_file.read_bytes(), before_bytes)
                self.assertEqual(self._table_names(database_file), before_tables)
                self.assertFalse(fake_user_home.exists())

    @patch("athlete_growth.adapters.scheduler.macos.sys.platform", "darwin")
    @patch("athlete_growth.adapters.scheduler.macos.subprocess.run")
    def test_uninstall_accepts_missing_data_home_and_profile(self, run) -> None:
        missing_home = Path(self.temporary_directory.name) / "never-initialized"
        fake_user_home = Path(self.temporary_directory.name) / "fake-user"
        with patch(
            "athlete_growth.adapters.scheduler.macos.Path.home",
            return_value=fake_user_home,
        ):
            result = host_tools.automation_uninstall_macos(missing_home)

        self.assertEqual(
            result,
            {"installed": False, "label": "com.athlete-growth.automation"},
        )
        self.assertFalse(missing_home.exists())
        self.assertFalse(fake_user_home.exists())
        run.assert_called_once()

    def _insert_notification(self, suffix: str, athlete_id: str) -> str:
        run_id = f"run-{suffix}"
        change_set_id = f"change-{suffix}"
        snapshot_id = f"snapshot-{suffix}"
        notification_id = f"notification-{suffix}"
        now = self.now.isoformat()
        with sqlite3.connect(self.layout.database_file) as connection:
            connection.execute(
                """
                INSERT INTO automation_sync_runs (
                    run_id, athlete_id, source, reason, status, started_at,
                    completed_at, error, request_key, lease_token, lease_owner,
                    lease_expires_at, updated_at
                ) VALUES (?, ?, 'garmin-cn', 'test', 'completed', ?, ?, NULL,
                          NULL, NULL, NULL, NULL, ?)
                """,
                (run_id, athlete_id, now, now, now),
            )
            connection.execute(
                """
                INSERT INTO data_change_sets (
                    change_set_id, run_id, athlete_id, source, record_ids_json,
                    affected_dates_json, projection_record_ids_json, created_at
                ) VALUES (?, ?, ?, 'garmin-cn', '["record"]', '["2026-10-26"]',
                          '["record"]', ?)
                """,
                (change_set_id, run_id, athlete_id, now),
            )
            connection.execute(
                """
                INSERT INTO analysis_snapshots (
                    snapshot_id, change_set_id, athlete_id, period, analysis_kind,
                    algorithm_version, report_json, risk_level, created_at
                ) VALUES (?, ?, ?, '2026-10-26', 'wellness', 'wellness-report/1.0',
                          '{"period":"2026-10-26"}', 'low', ?)
                """,
                (snapshot_id, change_set_id, athlete_id, now),
            )
            connection.execute(
                """
                INSERT INTO notification_inbox (
                    notification_id, snapshot_id, athlete_id, policy_version,
                    channel, priority, title, body, deliver_after, created_at,
                    read_at, delivered_at, delivery_attempts, last_error,
                    next_attempt_at, delivery_lease_token, delivery_lease_owner,
                    delivery_lease_expires_at
                ) VALUES (?, ?, ?, 'notification-policy/1.0', 'inbox', 'digest',
                          'Training analysis ready', 'Open the local report.', NULL, ?,
                          NULL, NULL, 0, NULL, ?, NULL, NULL, NULL)
                """,
                (notification_id, snapshot_id, athlete_id, now, now),
            )
        return notification_id

    def _initialize_profile_without_automation(self, suffix: str) -> Path:
        home = Path(self.temporary_directory.name) / suffix / "data-home"
        with redirect_stdout(StringIO()):
            code = cli_main.main(
                [
                    "init",
                    "--name",
                    suffix,
                    "--timezone",
                    "Asia/Shanghai",
                    "--data-home",
                    str(home),
                ]
            )
        self.assertEqual(code, 0)
        return home

    @staticmethod
    def _table_names(database_file: Path) -> tuple[str, ...]:
        with sqlite3.connect(database_file) as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        return tuple(row[0] for row in rows)


if __name__ == "__main__":
    unittest.main()
