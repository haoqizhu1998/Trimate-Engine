"""Idempotent raw and canonical data ingestion."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import sleep
from zoneinfo import ZoneInfo

from athlete_growth.adapters.storage.sqlite_records import SQLiteRecordStore
from athlete_growth.domain.identifiers import require_strict_child
from athlete_growth.domain.records import CanonicalRecord, EvidenceRef, JsonValue
from athlete_growth.ports.data_source import (
    DataSourceConnector,
    RawSourceRecord,
    SyncBatch,
    SyncWindow,
)


@dataclass(frozen=True, slots=True)
class IngestionResult:
    inserted: int
    updated: int
    retired: int
    duplicates: int
    raw_files: int
    changed_record_ids: tuple[str, ...] = ()
    affected_dates: tuple[date, ...] = ()
    staged_raw_files: tuple[tuple[Path, Path], ...] = field(
        default=(),
        repr=False,
        compare=False,
    )

    @property
    def has_changes(self) -> bool:
        return self.inserted > 0 or self.updated > 0 or self.retired > 0


class IngestionService:
    def __init__(
        self,
        *,
        athlete_id: str,
        store: SQLiteRecordStore,
        raw_dir: Path,
        timezone_name: str = "UTC",
    ) -> None:
        self.athlete_id = athlete_id
        self.store = store
        self.raw_dir = raw_dir
        self.timezone = ZoneInfo(timezone_name)

    def ingest(
        self,
        batch: SyncBatch,
        *,
        connection: sqlite3.Connection | None = None,
        raw_staging_dir: Path | None = None,
    ) -> IngestionResult:
        if raw_staging_dir is not None:
            require_strict_child(self.raw_dir, raw_staging_dir, "raw staging directory")
        inserted = 0
        updated = 0
        retired = 0
        duplicates = 0
        raw_files = 0
        sources: set[str] = set()
        changed_record_ids: set[str] = set()
        affected_dates: set[date] = set()
        staged_raw_files: list[tuple[Path, Path]] = []
        current_by_source: dict[tuple[str, str, str], list[CanonicalRecord]] = {}
        for current in self.store.list_current(
            self.athlete_id,
            connection=connection,
        ):
            for evidence in current.evidence:
                current_by_source.setdefault(
                    (evidence.source, current.kind, evidence.source_id), []
                ).append(current)
        for raw in batch.records:
            sources.add(raw.source)
            for artifact in raw.artifacts:
                artifact_digest = hashlib.sha256(artifact.content).hexdigest()
                if artifact.format == "fit-zip":
                    sync_metadata = raw.payload.get("_sync")
                    expected_digest = (
                        sync_metadata.get("original_fit_zip_sha256")
                        if isinstance(sync_metadata, dict)
                        else None
                    )
                    if expected_digest is not None and expected_digest != artifact_digest:
                        raise ValueError("source artifact hash does not match its metadata")
                artifact_destination = (
                    self.raw_dir / raw.source / "assets" / artifact_digest[:2]
                    / f"{artifact_digest}.zip"
                )
                created, staged_artifact = self._stage_raw_file(
                    artifact_destination,
                    artifact.content,
                    raw.source,
                    artifact_digest,
                    raw_root=self.raw_dir,
                    raw_staging_dir=raw_staging_dir,
                )
                raw_files += int(created)
                if staged_artifact is not None:
                    staged_raw_files.append((staged_artifact, artifact_destination))
            payload_bytes = json.dumps(
                raw.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            digest = hashlib.sha256(payload_bytes).hexdigest()
            destination = self.raw_dir / raw.source / digest[:2] / f"{digest}.json"
            raw_created, staged = self._stage_raw_file(
                destination,
                payload_bytes,
                raw.source,
                digest,
                raw_root=self.raw_dir,
                raw_staging_dir=raw_staging_dir,
            )
            raw_files += int(raw_created)
            if staged is not None:
                staged_raw_files.append((staged, destination))
            canonical_payload = raw.canonical_payload or raw.payload
            flags = self._quality_flags(canonical_payload)
            candidate = CanonicalRecord.create(
                athlete_id=self.athlete_id,
                kind=raw.kind,
                occurred_at=raw.occurred_at,
                data=canonical_payload,
                evidence=(
                    EvidenceRef(
                        source=raw.source,
                        source_id=raw.source_id,
                        captured_at=datetime.now(UTC),
                        raw_hash=digest,
                    ),
                ),
                quality_flags=flags,
            )
            source_key = (raw.source, raw.kind, raw.source_id)
            if raw.kind == "activity" and source_key not in current_by_source and any(
                self._same_activity_from_other_source(record, candidate, raw.source)
                for versions in current_by_source.values()
                for record in versions
                if record.kind == "activity"
            ):
                # Keep the immutable source payload in Raw Vault, but do not
                # count the same workout twice in load and progress reports.
                duplicates += 1
                continue
            retired_records = self._retire_legacy_daily_duplicates(
                raw,
                canonical_payload,
                current_by_source,
                connection=connection,
            )
            retired += len(retired_records)
            for record in retired_records:
                changed_record_ids.add(record.record_id)
                affected_dates.add(self._affected_date(record))
            existing_versions = current_by_source.get(source_key, [])
            existing = next(
                (record for record in existing_versions if self._equivalent(record, candidate)),
                existing_versions[-1] if existing_versions else None,
            )
            for extra in existing_versions:
                if extra is existing:
                    continue
                self.store.retire(extra.record_id, connection=connection)
                retired += 1
                changed_record_ids.add(extra.record_id)
                affected_dates.add(self._affected_date(extra))
            if existing is not None and self._equivalent(existing, candidate):
                current_by_source[source_key] = [existing]
                duplicates += 1
                continue
            if existing is not None:
                replacement = CanonicalRecord.create(
                    athlete_id=self.athlete_id,
                    kind=raw.kind,
                    occurred_at=raw.occurred_at,
                    data=canonical_payload,
                    evidence=candidate.evidence,
                    quality_flags=flags,
                    supersedes_id=existing.record_id,
                )
                self.store.correct(
                    existing.record_id,
                    replacement,
                    connection=connection,
                )
                current_by_source[source_key] = [replacement]
                updated += 1
                changed_record_ids.add(replacement.record_id)
                affected_dates.add(self._affected_date(existing))
                affected_dates.add(self._affected_date(replacement))
            elif self.store.save(candidate, connection=connection):
                current_by_source[source_key] = [candidate]
                inserted += 1
                changed_record_ids.add(candidate.record_id)
                affected_dates.add(self._affected_date(candidate))
            else:
                duplicates += 1
        if batch.next_cursor is not None:
            for source in sources:
                self.store.set_cursor(
                    self.athlete_id,
                    source,
                    batch.next_cursor,
                    datetime.now(UTC),
                    connection=connection,
                )
        return IngestionResult(
            inserted,
            updated,
            retired,
            duplicates,
            raw_files,
            changed_record_ids=tuple(sorted(changed_record_ids)),
            affected_dates=tuple(sorted(affected_dates)),
            staged_raw_files=tuple(staged_raw_files),
        )

    @staticmethod
    def _stage_raw_file(
        destination: Path,
        payload: bytes,
        source: str,
        digest: str,
        *,
        raw_root: Path,
        raw_staging_dir: Path | None,
    ) -> tuple[bool, Path | None]:
        require_strict_child(raw_root, destination, "raw destination")
        if destination.exists():
            return False, None
        target = (
            destination
            if raw_staging_dir is None
            else raw_staging_dir / destination.relative_to(raw_root)
        )
        require_strict_child(raw_root, target, "raw staging target")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target.parent, 0o700)
        if target.exists():
            return False, None
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False, None
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        return True, target if raw_staging_dir is not None else None

    @staticmethod
    def promote_raw_files(result: IngestionResult) -> None:
        """Publish staged immutable raw files before the enclosing DB commit."""

        for staged, destination in result.staged_raw_files:
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(destination.parent, 0o700)
            if destination.exists():
                staged.unlink(missing_ok=True)
                continue
            os.replace(staged, destination)
            os.chmod(destination, 0o600)

    def _retire_legacy_daily_duplicates(
        self,
        raw: RawSourceRecord,
        canonical_payload: Mapping[str, JsonValue],
        current_by_source: dict[tuple[str, str, str], list[CanonicalRecord]],
        *,
        connection: sqlite3.Connection | None,
    ) -> tuple[CanonicalRecord, ...]:
        """Replace legacy per-day fragments once a canonical daily row arrives."""
        if raw.kind == "body-weight" and ":body-weight:" not in raw.source_id:
            return self._retire_matching_source_ids(
                raw,
                current_by_source,
                lambda source_id: ":body-weight:" in source_id,
                connection=connection,
            )
        daily_kinds = {"steps", "sleep", "hrv", "heart-rate", "stress", "body-battery"}
        calendar_date = canonical_payload.get("calendar_date")
        if (
            raw.kind not in daily_kinds
            or not isinstance(calendar_date, str)
            or raw.source_id != calendar_date
        ):
            return ()
        legacy_prefix = f"{calendar_date}:{raw.kind}:"
        return self._retire_matching_source_ids(
            raw,
            current_by_source,
            lambda source_id: source_id.startswith(legacy_prefix),
            connection=connection,
        )

    def _retire_matching_source_ids(
        self,
        raw: RawSourceRecord,
        current_by_source: dict[tuple[str, str, str], list[CanonicalRecord]],
        matches: Callable[[str], bool],
        *,
        connection: sqlite3.Connection | None,
    ) -> tuple[CanonicalRecord, ...]:
        retired: list[CanonicalRecord] = []
        for source_key in list(current_by_source):
            source, kind, source_id = source_key
            if source != raw.source or kind != raw.kind or not matches(source_id):
                continue
            for record in current_by_source.pop(source_key):
                self.store.retire(record.record_id, connection=connection)
                retired.append(record)
        return tuple(retired)

    @staticmethod
    def _same_activity_from_other_source(
        existing: CanonicalRecord, candidate: CanonicalRecord, source: str
    ) -> bool:
        shared_activity_sources = {"garmin-cn", "garmin-global", "coros"}
        if source not in shared_activity_sources or not any(
            item.source in shared_activity_sources and item.source != source
            for item in existing.evidence
        ):
            return False
        if abs((existing.occurred_at - candidate.occurred_at).total_seconds()) > 2:
            return False
        for key, tolerance in (("duration_min", 1 / 60), ("distance_m", 1.0)):
            left, right = existing.data.get(key), candidate.data.get(key)
            if (
                isinstance(left, bool) or not isinstance(left, (int, float))
                or isinstance(right, bool) or not isinstance(right, (int, float))
                or abs(left - right) > tolerance
            ):
                return False
        return True

    @staticmethod
    def _equivalent(existing: CanonicalRecord, candidate: CanonicalRecord) -> bool:
        def evidence_identity(record: CanonicalRecord) -> tuple[tuple[str, str, str | None], ...]:
            return tuple((item.source, item.source_id, item.raw_hash) for item in record.evidence)

        return (
            existing.athlete_id == candidate.athlete_id
            and existing.kind == candidate.kind
            and existing.occurred_at == candidate.occurred_at
            and existing.data == candidate.data
            and existing.quality_flags == candidate.quality_flags
            and existing.schema_version == candidate.schema_version
            and evidence_identity(existing) == evidence_identity(candidate)
        )

    def _affected_date(self, record: CanonicalRecord) -> date:
        calendar_date = record.data.get("calendar_date")
        if isinstance(calendar_date, str):
            try:
                return date.fromisoformat(calendar_date)
            except ValueError:
                pass
        return record.occurred_at.astimezone(self.timezone).date()

    def fetch(
        self,
        connector: DataSourceConnector,
        window: SyncWindow,
        *,
        max_attempts: int = 3,
    ) -> SyncBatch:
        """Fetch one batch with bounded retries and without mutating local state."""

        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        source = connector.capabilities().source
        cursor = self.store.get_cursor(self.athlete_id, source)
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            try:
                return connector.sync(window, cursor)
            except (ConnectionError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt + 1 < max_attempts:
                    sleep(0.05 * (2**attempt))
        assert last_error is not None
        raise last_error

    def sync(
        self,
        connector: DataSourceConnector,
        window: SyncWindow,
        *,
        max_attempts: int = 3,
    ) -> IngestionResult:
        return self.ingest(self.fetch(connector, window, max_attempts=max_attempts))

    def sync_chunked(
        self,
        connector: DataSourceConnector,
        window: SyncWindow,
        *,
        chunk_days: int = 28,
    ) -> IngestionResult:
        """Persist a long sync window in bounded, idempotent chunks."""
        if chunk_days < 1:
            raise ValueError("chunk_days must be at least 1")
        totals = [0, 0, 0, 0, 0]
        changed_record_ids: set[str] = set()
        affected_dates: set[date] = set()
        chunk_start = window.start
        while chunk_start <= window.end:
            chunk_end = min(
                chunk_start + timedelta(days=chunk_days - 1),
                window.end,
            )
            result = self.sync(connector, SyncWindow(chunk_start, chunk_end))
            for index, value in enumerate(
                (
                    result.inserted,
                    result.updated,
                    result.retired,
                    result.duplicates,
                    result.raw_files,
                )
            ):
                totals[index] += value
            changed_record_ids.update(result.changed_record_ids)
            affected_dates.update(result.affected_dates)
            chunk_start = chunk_end + timedelta(days=1)
        return IngestionResult(
            *totals,
            changed_record_ids=tuple(sorted(changed_record_ids)),
            affected_dates=tuple(sorted(affected_dates)),
        )

    @staticmethod
    def _quality_flags(payload: Mapping[str, JsonValue]) -> tuple[str, ...]:
        non_negative_metrics = {
            "avg_hr",
            "avg_speed_mps",
            "avg_power_w",
            "avg_cadence_rpm",
            "body_battery",
            "calories",
            "distance_m",
            "duration_min",
            "elevation_gain_m",
            "high_intensity_min",
            "hrv",
            "max_hr",
            "resting_hr",
            "sleep_hours",
            "sleep_seconds",
            "steps",
            "training_load",
            "weight_kg",
        }
        flags: list[str] = []
        for key, value in payload.items():
            if key in non_negative_metrics and isinstance(value, (int, float)) and value < 0:
                flags.append(f"negative:{key}")
        return tuple(flags)
