"""One-shot orchestration for durable automatic training updates."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

from athlete_growth.adapters.storage.sqlite_records import SQLiteRecordStore
from athlete_growth.application.automation.models import (
    LEGACY_PROJECTION_UNAVAILABLE,
    AnalysisSnapshot,
    DataChangeSet,
    NotificationMode,
    SyncRun,
    SyncSubscription,
)
from athlete_growth.application.ingest.service import IngestionService
from athlete_growth.application.report.service import ReportService, WellnessReport
from athlete_growth.domain.identifiers import require_strict_child
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
        if automation_repository.database_file.resolve() != record_store.database_file.resolve():
            raise ValueError("automation and record repositories must use the same database")
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
        claimed_count = recovered_count = 0
        completed = changed = no_changes = failed = snapshots = notifications = 0
        skipped_recoveries: set[str] = set()
        processed_subscriptions: set[tuple[str, str]] = set()
        for _ in range(limit):
            claimed = self._claim_next(skipped_recoveries, processed_subscriptions)
            if claimed is None:
                break
            subscription, run, recovered = claimed
            processed_subscriptions.add((subscription.athlete_id, subscription.source))
            claimed_count += 1
            recovered_count += int(recovered)
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
            claimed=claimed_count,
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

    def _claim_next(
        self,
        skipped_recoveries: set[str],
        processed_subscriptions: set[tuple[str, str]],
    ) -> tuple[SyncSubscription, SyncRun, bool] | None:
        """Acquire at most one run using a fresh clock reading."""

        now = self._now()
        for expired in self.automation_repository.list_recoverable_runs(now):
            subscription_key = (expired.athlete_id, expired.source)
            if expired.run_id in skipped_recoveries or subscription_key in processed_subscriptions:
                continue
            subscription = self.automation_repository.get_subscription(
                expired.athlete_id, expired.source
            )
            if subscription is None:
                skipped_recoveries.add(expired.run_id)
                continue
            try:
                reclaimed = self.automation_repository.reclaim_sync_run(
                    expired.run_id,
                    now=self._now(),
                    owner=self.owner,
                    lease_duration=self.lease_duration,
                    lease_clock=self._now,
                )
            except (KeyError, ValueError):
                skipped_recoveries.add(expired.run_id)
                continue
            return subscription, reclaimed, True

        claims = self.automation_repository.claim_due(
            self._now(),
            owner=self.owner,
            lease_duration=self.lease_duration,
            limit=1,
            exclude=processed_subscriptions,
            lease_clock=self._now,
        )
        if not claims:
            return None
        subscription, run = claims[0]
        return subscription, run, False

    def deliver_pending(self, *, limit: int = 100) -> DeliverySummary:
        """Claim and attempt a bounded due batch.

        Delivery is at-least-once: claims prevent concurrent sends, but a process
        crash after macOS accepts a notification and before the receipt is saved
        can cause a rare replay because the OS sink has no idempotency key.
        """

        self._positive_limit(limit, "limit")
        attempted = delivered = failed = 0
        for _ in range(limit):
            claimed = self.automation_repository.claim_pending(
                self._now(),
                owner=self.owner,
                lease_duration=self.lease_duration,
                limit=1,
                lease_clock=self._now,
            )
            if not claimed:
                break
            notification = claimed[0]
            attempted += 1
            assert notification.delivery_lease_token is not None
            try:
                notification = self.automation_repository.renew_delivery_claim(
                    notification.notification_id,
                    lease_token=notification.delivery_lease_token,
                    owner=self.owner,
                    now=self._now(),
                    lease_duration=self.lease_duration,
                    lease_clock=self._now,
                )
            except Exception:
                failed += 1
                continue
            try:
                self.notification_sink.deliver(notification)
            except Exception as exc:
                # The claim may expire or be recovered during external delivery.
                with suppress(Exception):
                    self.automation_repository.mark_delivery_failed(
                        notification.notification_id,
                        f"{type(exc).__name__}: delivery failed",
                        failed_at=self._now(),
                        lease_token=notification.delivery_lease_token,
                        owner=self.owner,
                    )
                failed += 1
                continue
            try:
                self.automation_repository.mark_delivered(
                    notification.notification_id,
                    delivered_at=self._now(),
                    lease_token=notification.delivery_lease_token,
                    owner=self.owner,
                )
            except Exception:
                failed += 1
                continue
            delivered += 1
        return DeliverySummary(attempted=attempted, delivered=delivered, failed=failed)

    def _process(self, subscription: SyncSubscription, run: SyncRun) -> _RunOutcome:
        profile = self.profile_repository.get_by_id(subscription.athlete_id)
        if profile is None:
            raise ValueError("athlete profile is unavailable")
        if run.lease_token is None:
            raise ValueError("automatic sync run is missing a lease token")

        run = self._renew_owned(run)
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
        athlete_raw_root = require_strict_child(
            self.raw_root,
            self.raw_root / subscription.athlete_id,
            "athlete raw root",
        )
        ingestion = IngestionService(
            athlete_id=subscription.athlete_id,
            store=self.record_store,
            raw_dir=athlete_raw_root,
            timezone_name=profile.timezone_name,
        )
        batch = ingestion.fetch(connector, window)
        if any(raw.source != subscription.source for raw in batch.records):
            raise ValueError("sync batch source does not match subscription")
        run = self._renew_owned(run)
        assert run.lease_token is not None
        athlete_raw_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (
            TemporaryDirectory(
                prefix=".staging-",
                dir=athlete_raw_root,
            ) as staging_directory,
            self.automation_repository.lease_fenced_transaction(
                run.run_id,
                lease_token=run.lease_token,
                owner=self.owner,
                now=self._now(),
                lease_clock=self._now,
            ) as connection,
        ):
            result = ingestion.ingest(
                batch,
                connection=connection,
                raw_staging_dir=Path(staging_directory),
            )
            self.automation_repository.validate_lease(
                run.run_id,
                lease_token=run.lease_token,
                owner=self.owner,
                now=self._now(),
                connection=connection,
            )
            projection_record_ids = self.record_store.list_current_ids(
                subscription.athlete_id,
                connection=connection,
            )
            change_set = (
                self.automation_repository.save_change_set(
                    run.run_id,
                    result,
                    created_at=self._now(),
                    lease_token=run.lease_token,
                    connection=connection,
                    lease_clock=self._now,
                    projection_record_ids=projection_record_ids,
                )
                if result.has_changes
                else None
            )
            ingestion.promote_raw_files(result)
        run = self._renew_owned(run)
        if not result.has_changes:
            self.automation_repository.complete_sync_run(
                run.run_id,
                completed_at=self._now(),
                lease_token=run.lease_token,
                lease_clock=self._now,
            )
            return _RunOutcome(changed=False)

        assert change_set is not None
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
        self._verify_raw_evidence(change_set)
        persisted_snapshots = self.automation_repository.list_snapshots_for_change_set(
            change_set.change_set_id
        )
        algorithm_version = "wellness-report/1.0"
        persisted_by_identity = {
            (snapshot.analysis_kind, snapshot.period, snapshot.algorithm_version): snapshot
            for snapshot in persisted_snapshots
        }
        report_service = ReportService(
            subscription.athlete_id,
            self.record_store,
            timezone_name=timezone_name,
            projection_record_ids=change_set.projection_record_ids,
        )
        snapshots: list[AnalysisSnapshot] = []
        for affected_date in change_set.affected_dates:
            identity = ("daily", affected_date.isoformat(), algorithm_version)
            persisted = persisted_by_identity.get(identity)
            if persisted is not None:
                snapshots.append(persisted)
                continue
            report = report_service.daily(affected_date)
            run = self._renew_owned(run)
            snapshots.append(self._save_report(run, change_set, report, analysis_kind="daily"))
        latest_date = change_set.affected_dates[-1]
        weekly_identity = (
            "weekly",
            self._weekly_period(latest_date, timezone_name),
            algorithm_version,
        )
        persisted_weekly = persisted_by_identity.get(weekly_identity)
        if persisted_weekly is not None:
            snapshots.append(persisted_weekly)
        else:
            weekly = report_service.weekly(latest_date)
            run = self._renew_owned(run)
            snapshots.append(self._save_report(run, change_set, weekly, analysis_kind="weekly"))

        notifications = self.automation_repository.list_notifications_for_change_set(
            change_set.change_set_id
        )
        if subscription.notification_mode is not NotificationMode.OFF and not notifications:
            selected = self._select_persisted_snapshot(snapshots)
            risk_level = selected.risk_level
            actionable = risk_level in {"moderate", "high"}
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
            run = self._renew_owned(run)
            assert run.lease_token is not None
            effect_at = self._now()
            notifications = [
                self.automation_repository.enqueue_notification(
                    selected,
                    priority="actionable" if actionable else "digest",
                    deliver_after=deliver_after,
                    title=f"Training data updated: {risk_level} risk",
                    body=self._persisted_notification_body(
                        len(change_set.record_ids),
                        risk_level,
                        selected.report,
                    ),
                    created_at=effect_at,
                    run_id=run.run_id,
                    lease_token=run.lease_token,
                    lease_owner=self.owner,
                    lease_checked_at=effect_at,
                    lease_clock=self._now,
                )
            ]

        run = self._renew_owned(run)
        assert run.lease_token is not None
        self.automation_repository.complete_sync_run(
            run.run_id,
            completed_at=self._now(),
            lease_token=run.lease_token,
            lease_clock=self._now,
        )
        return _RunOutcome(
            changed=True,
            snapshots=len(snapshots),
            notifications=len(notifications),
        )

    @staticmethod
    def _select_persisted_snapshot(
        snapshots: list[AnalysisSnapshot],
    ) -> AnalysisSnapshot:
        risk_order = {"unknown": 0, "low": 1, "moderate": 2, "high": 3}

        def affected_date(snapshot: AnalysisSnapshot) -> date:
            if snapshot.analysis_kind != "daily":
                return date.min
            try:
                return date.fromisoformat(snapshot.period)
            except ValueError:
                return date.min

        return max(
            snapshots,
            key=lambda snapshot: (
                risk_order[snapshot.risk_level],
                snapshot.analysis_kind == "daily",
                affected_date(snapshot),
                snapshot.snapshot_id,
            ),
        )

    def _save_report(
        self,
        run: SyncRun,
        change_set: DataChangeSet,
        report: WellnessReport,
        *,
        analysis_kind: str,
    ) -> AnalysisSnapshot:
        assert run.lease_token is not None
        effect_at = self._now()
        return self.automation_repository.save_snapshot(
            change_set,
            cast(dict[str, JsonValue], self._json_value(asdict(report))),
            report.safety.level.value,
            created_at=effect_at,
            analysis_kind=analysis_kind,
            algorithm_version=report.algorithm_version,
            run_id=run.run_id,
            lease_token=run.lease_token,
            lease_owner=self.owner,
            lease_checked_at=effect_at,
            lease_clock=self._now,
        )

    @staticmethod
    def _weekly_period(end_date: date, timezone_name: str) -> str:
        ZoneInfo(timezone_name)
        start_date = end_date - timedelta(days=6)
        return f"{start_date.isoformat()}/{end_date.isoformat()}"

    def _verify_raw_evidence(self, change_set: DataChangeSet) -> None:
        """Refuse analysis completion when a referenced immutable raw object is absent."""

        if LEGACY_PROJECTION_UNAVAILABLE in change_set.projection_record_ids:
            raise ValueError("legacy analysis projection is unavailable; sync must be restarted")
        referenced_record_ids = tuple(
            dict.fromkeys(change_set.record_ids + change_set.projection_record_ids)
        )
        for record_id in referenced_record_ids:
            record = self.record_store.get(record_id)
            if record is None:
                raise ValueError("change set record is unavailable")
            for evidence in record.evidence:
                if evidence.raw_hash is None:
                    continue
                raw_file = (
                    self.raw_root
                    / change_set.athlete_id
                    / evidence.source
                    / evidence.raw_hash[:2]
                    / f"{evidence.raw_hash}.json"
                )
                if not raw_file.is_file():
                    raise ValueError("raw evidence is unavailable")

    def _fail_if_owned(self, run: SyncRun, error: Exception) -> None:
        if run.lease_token is None:
            return
        try:
            run = self._renew_owned(run)
            assert run.lease_token is not None
            self.automation_repository.fail_sync_run(
                run.run_id,
                error=f"{type(error).__name__}: automatic sync failed",
                completed_at=self._now(),
                lease_token=run.lease_token,
                lease_clock=self._now,
            )
        except Exception:
            # A concurrent recovery may already own or have finished this run.
            return

    def _renew_owned(self, run: SyncRun) -> SyncRun:
        """Revalidate ownership and renew only when the lease is near expiry."""

        if run.lease_token is None:
            raise ValueError("automatic sync run is missing a lease token")
        now = self._now()
        current = self.automation_repository.validate_lease(
            run.run_id,
            lease_token=run.lease_token,
            owner=self.owner,
            now=now,
        )
        assert current.lease_expires_at is not None
        assert current.lease_token is not None
        if current.lease_expires_at - now > self.lease_duration / 3:
            return current
        return self.automation_repository.renew_lease(
            current.run_id,
            current.lease_token,
            owner=self.owner,
            now=now,
            lease_duration=self.lease_duration,
            lease_clock=self._now,
        )

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
        for day_offset in range(2):
            local_candidate = datetime.combine(
                local_now.date() + timedelta(days=day_offset),
                time(hour),
            )
            candidates = AutomaticTrainingLoop._valid_local_instants(
                local_candidate,
                timezone,
            )
            if not candidates:
                for minute_offset in range(1, 24 * 60 + 1):
                    candidates = AutomaticTrainingLoop._valid_local_instants(
                        local_candidate + timedelta(minutes=minute_offset),
                        timezone,
                    )
                    if candidates:
                        break
            eligible = [candidate for candidate in candidates if candidate >= now]
            if eligible:
                return min(eligible)
        raise ValueError("could not resolve next digest time")

    @staticmethod
    def _valid_local_instants(local_time: datetime, timezone: ZoneInfo) -> list[datetime]:
        candidates: set[datetime] = set()
        for fold in (0, 1):
            aware = local_time.replace(tzinfo=timezone, fold=fold)
            utc_candidate = aware.astimezone(UTC)
            round_trip = utc_candidate.astimezone(timezone)
            if round_trip.replace(tzinfo=None) == local_time and round_trip.fold == fold:
                candidates.add(utc_candidate)
        return sorted(candidates)

    @staticmethod
    def _persisted_notification_body(
        changed_records: int,
        risk_level: str,
        report: Mapping[str, object],
    ) -> str:
        next_step: str | None = None
        safety = report.get("safety")
        if isinstance(safety, Mapping):
            actions = safety.get("actions")
            if (
                isinstance(actions, Sequence)
                and not isinstance(actions, str | bytes)
                and actions
                and isinstance(actions[0], str)
            ):
                next_step = actions[0]
        if next_step is None:
            gaps = report.get("data_gaps")
            if (
                isinstance(gaps, Sequence)
                and not isinstance(gaps, str | bytes)
                and gaps
                and isinstance(gaps[0], str)
            ):
                next_step = f"Review data gap: {gaps[0]}."
        if next_step is None:
            next_step = "Review the latest training analysis."
        return f"{changed_records} record(s) changed. Risk: {risk_level}. {next_step}"
