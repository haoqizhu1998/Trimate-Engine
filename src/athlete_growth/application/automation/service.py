"""One-shot orchestration for durable automatic training updates."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

from athlete_growth.adapters.storage.sqlite_records import SQLiteRecordStore
from athlete_growth.application.automation.models import (
    AnalysisSnapshot,
    DataChangeSet,
    NotificationMode,
    SyncRun,
    SyncSubscription,
)
from athlete_growth.application.ingest.service import IngestionService
from athlete_growth.application.report.service import ReportService, WellnessReport
from athlete_growth.domain.records import JsonValue
from athlete_growth.ports.data_source import DataSourceConnector, SyncWindow
from athlete_growth.ports.notification import NoopNotificationSink, NotificationSink
from athlete_growth.ports.repositories import AthleteProfileRepository

if TYPE_CHECKING:
    from athlete_growth.adapters.storage.sqlite_automation import SQLiteAutomationRepository


@dataclass(frozen=True, slots=True)
class DeliverySummary:
    attempted: int = 0
    delivered: int = 0
    failed: int = 0


@dataclass(frozen=True, slots=True)
class AutomationSummary:
    claimed: int = 0
    recovered: int = 0
    completed: int = 0
    changed: int = 0
    no_changes: int = 0
    failed: int = 0
    snapshots: int = 0
    notifications: int = 0
    delivered: int = 0
    delivery_failures: int = 0


@dataclass(frozen=True, slots=True)
class _RunOutcome:
    changed: bool
    snapshots: int = 0
    notifications: int = 0


class AutomaticTrainingLoop:
    """Claim bounded work, sync it, analyze real changes, and deliver due notices."""

    def __init__(
        self,
        *,
        automation_repository: SQLiteAutomationRepository,
        record_store: SQLiteRecordStore,
        profile_repository: AthleteProfileRepository,
        connectors: Mapping[str, DataSourceConnector],
        raw_root: Path,
        notification_sink: NotificationSink | None = None,
        clock: Callable[[], datetime] | None = None,
        owner: str = "automatic-training-loop",
        lease_duration: timedelta = timedelta(minutes=15),
    ) -> None:
        if not owner.strip():
            raise ValueError("owner must not be blank")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        self.automation_repository = automation_repository
        self.record_store = record_store
        self.profile_repository = profile_repository
        self.connectors = dict(connectors)
        self.raw_root = raw_root
        self.notification_sink = notification_sink or NoopNotificationSink()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.owner = owner
        self.lease_duration = lease_duration

    def run_once(
        self,
        *,
        limit: int = 100,
        delivery_limit: int = 100,
    ) -> AutomationSummary:
        """Run one bounded pass; errors are persisted per run and do not stop peers."""

        self._positive_limit(limit, "limit")
        self._positive_limit(delivery_limit, "delivery_limit")
        now = self._now()
        work: list[tuple[SyncSubscription, SyncRun]] = []
        recovered_count = 0

        for expired in self.automation_repository.list_recoverable_runs(now)[:limit]:
            subscription = self.automation_repository.get_subscription(
                expired.athlete_id, expired.source
            )
            if subscription is None:
                continue
            try:
                reclaimed = self.automation_repository.reclaim_sync_run(
                    expired.run_id,
                    now=now,
                    owner=self.owner,
                    lease_duration=self.lease_duration,
                )
            except (KeyError, ValueError):
                continue
            work.append((subscription, reclaimed))
            recovered_count += 1

        remaining = limit - len(work)
        if remaining:
            work.extend(
                self.automation_repository.claim_due(
                    now,
                    owner=self.owner,
                    lease_duration=self.lease_duration,
                    limit=remaining,
                )
            )

        completed = changed = no_changes = failed = snapshots = notifications = 0
        for subscription, run in work:
            try:
                outcome = self._process(subscription, run)
            except Exception as exc:
                failed += 1
                self._fail_if_owned(run, exc)
                continue
            completed += 1
            changed += int(outcome.changed)
            no_changes += int(not outcome.changed)
            snapshots += outcome.snapshots
            notifications += outcome.notifications

        delivery = self.deliver_pending(limit=delivery_limit)
        return AutomationSummary(
            claimed=len(work),
            recovered=recovered_count,
            completed=completed,
            changed=changed,
            no_changes=no_changes,
            failed=failed,
            snapshots=snapshots,
            notifications=notifications,
            delivered=delivery.delivered,
            delivery_failures=delivery.failed,
        )

    def deliver_pending(self, *, limit: int = 100) -> DeliverySummary:
        """Attempt each due pending notification once during this pass."""

        self._positive_limit(limit, "limit")
        due = self.automation_repository.list_pending(self._now())[:limit]
        delivered = failed = 0
        for notification in due:
            try:
                self.notification_sink.deliver(notification)
            except Exception as exc:
                self.automation_repository.mark_delivery_failed(
                    notification.notification_id,
                    f"{type(exc).__name__}: delivery failed",
                )
                failed += 1
                continue
            self.automation_repository.mark_delivered(
                notification.notification_id,
                delivered_at=self._now(),
            )
            delivered += 1
        return DeliverySummary(attempted=len(due), delivered=delivered, failed=failed)

    def _process(self, subscription: SyncSubscription, run: SyncRun) -> _RunOutcome:
        profile = self.profile_repository.get_by_id(subscription.athlete_id)
        if profile is None:
            raise ValueError("athlete profile is unavailable")
        if run.lease_token is None:
            raise ValueError("automatic sync run is missing a lease token")

        run = self.automation_repository.renew_lease(
            run.run_id,
            run.lease_token,
            owner=self.owner,
            now=self._now(),
            lease_duration=self.lease_duration * 2,
        )
        assert run.lease_token is not None
        existing_change_set = self.automation_repository.get_change_set_for_run(run.run_id)
        if existing_change_set is not None:
            return self._analyze_and_complete(
                subscription,
                run,
                existing_change_set,
                timezone_name=profile.timezone_name,
            )

        connector = self.connectors.get(subscription.source)
        if connector is None:
            raise ValueError("data source connector is unavailable")
        if connector.capabilities().source != subscription.source:
            raise ValueError("data source connector does not match subscription")
        local_today = self._now().astimezone(ZoneInfo(profile.timezone_name)).date()
        window = SyncWindow(
            local_today - timedelta(days=subscription.lookback_days - 1),
            local_today,
        )
        ingestion = IngestionService(
            athlete_id=subscription.athlete_id,
            store=self.record_store,
            raw_dir=self.raw_root / subscription.athlete_id,
        )
        result = ingestion.sync(connector, window)
        if not result.has_changes:
            self.automation_repository.complete_sync_run(
                run.run_id,
                completed_at=self._now(),
                lease_token=run.lease_token,
            )
            return _RunOutcome(changed=False)

        change_set = self.automation_repository.save_change_set(
            run.run_id,
            result,
            created_at=self._now(),
            lease_token=run.lease_token,
        )
        return self._analyze_and_complete(
            subscription,
            run,
            change_set,
            timezone_name=profile.timezone_name,
        )

    def _analyze_and_complete(
        self,
        subscription: SyncSubscription,
        run: SyncRun,
        change_set: DataChangeSet,
        *,
        timezone_name: str,
    ) -> _RunOutcome:
        assert run.lease_token is not None
        report_service = ReportService(
            subscription.athlete_id,
            self.record_store,
            timezone_name=timezone_name,
        )
        daily_results: list[tuple[AnalysisSnapshot, WellnessReport]] = []
        for affected_date in change_set.affected_dates:
            report = report_service.daily(affected_date)
            daily_results.append(
                (self._save_report(change_set, report, analysis_kind="daily"), report)
            )
        latest_date = change_set.affected_dates[-1]
        weekly = report_service.weekly(latest_date)
        self._save_report(change_set, weekly, analysis_kind="weekly")

        notification_count = 0
        if subscription.notification_mode is not NotificationMode.OFF:
            notification_snapshot, notification_report = daily_results[-1]
            risk_level = notification_report.safety.level.value
            actionable = risk_level in {"moderate", "high"}
            priority = "actionable" if actionable else "digest"
            deliver_after = None
            if (
                subscription.notification_mode is NotificationMode.ACTIONABLE_DIGEST
                and not actionable
            ):
                deliver_after = self._next_digest(
                    change_set.created_at,
                    timezone_name,
                    subscription.digest_hour_local,
                )
            self.automation_repository.enqueue_notification(
                notification_snapshot,
                priority=priority,
                deliver_after=deliver_after,
                title=f"Training data updated: {risk_level} risk",
                body=self._notification_body(
                    len(change_set.record_ids),
                    risk_level,
                    notification_report,
                ),
                created_at=self._now(),
            )
            notification_count = 1

        self.automation_repository.complete_sync_run(
            run.run_id,
            completed_at=self._now(),
            lease_token=run.lease_token,
        )
        return _RunOutcome(
            changed=True,
            snapshots=len(daily_results) + 1,
            notifications=notification_count,
        )

    def _save_report(
        self,
        change_set: DataChangeSet,
        report: WellnessReport,
        *,
        analysis_kind: str,
    ) -> AnalysisSnapshot:
        return self.automation_repository.save_snapshot(
            change_set,
            cast(dict[str, JsonValue], self._json_value(asdict(report))),
            report.safety.level.value,
            created_at=self._now(),
            analysis_kind=analysis_kind,
            algorithm_version=report.algorithm_version,
        )

    def _fail_if_owned(self, run: SyncRun, error: Exception) -> None:
        if run.lease_token is None:
            return
        try:
            self.automation_repository.fail_sync_run(
                run.run_id,
                error=f"{type(error).__name__}: automatic sync failed",
                completed_at=self._now(),
                lease_token=run.lease_token,
            )
        except (KeyError, ValueError):
            # A concurrent recovery may already own or have finished this run.
            return

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now

    @staticmethod
    def _positive_limit(value: int, field_name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{field_name} must be a positive integer")

    @classmethod
    def _json_value(cls, value: object) -> JsonValue:
        if isinstance(value, Enum):
            return cast(str, value.value)
        if isinstance(value, datetime | date):
            return value.isoformat()
        if is_dataclass(value) and not isinstance(value, type):
            return cls._json_value(asdict(value))
        if isinstance(value, Mapping):
            return {str(key): cls._json_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._json_value(item) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        raise ValueError(f"report contains unsupported value: {type(value).__name__}")

    @staticmethod
    def _next_digest(now: datetime, timezone_name: str, hour: int) -> datetime:
        timezone = ZoneInfo(timezone_name)
        local_now = now.astimezone(timezone)
        candidate = datetime.combine(local_now.date(), time(hour), tzinfo=timezone)
        if candidate <= local_now:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)

    @staticmethod
    def _notification_body(
        changed_records: int,
        risk_level: str,
        report: WellnessReport,
    ) -> str:
        if report.safety.actions:
            next_step = report.safety.actions[0]
        elif report.data_gaps:
            next_step = f"Review data gap: {report.data_gaps[0]}."
        else:
            next_step = "Review the latest training analysis."
        return f"{changed_records} record(s) changed. Risk: {risk_level}. {next_step}"
