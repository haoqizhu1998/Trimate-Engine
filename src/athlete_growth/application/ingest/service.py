"""Idempotent raw and canonical data ingestion."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import sleep

from athlete_growth.adapters.storage.sqlite_records import SQLiteRecordStore
from athlete_growth.domain.records import CanonicalRecord, EvidenceRef, JsonValue
from athlete_growth.ports.data_source import DataSourceConnector, SyncBatch, SyncWindow


@dataclass(frozen=True, slots=True)
class IngestionResult:
    inserted: int
    updated: int
    retired: int
    duplicates: int
    raw_files: int


class IngestionService:
    def __init__(self, *, athlete_id: str, store: SQLiteRecordStore, raw_dir: Path) -> None:
        self.athlete_id = athlete_id
        self.store = store
        self.raw_dir = raw_dir

    def ingest(self, batch: SyncBatch) -> IngestionResult:
        inserted = 0
        updated = 0
        retired = 0
        duplicates = 0
        raw_files = 0
        sources: set[str] = set()
        current_by_source: dict[tuple[str, str, str], list[CanonicalRecord]] = {}
        for current in self.store.list_current(self.athlete_id):
            for evidence in current.evidence:
                current_by_source.setdefault(
                    (evidence.source, current.kind, evidence.source_id), []
                ).append(current)
        for raw in batch.records:
            sources.add(raw.source)
            payload_bytes = json.dumps(
                raw.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            digest = hashlib.sha256(payload_bytes).hexdigest()
            destination = self.raw_dir / raw.source / digest[:2] / f"{digest}.json"
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(destination.parent, 0o700)
            if not destination.exists():
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload_bytes)
                raw_files += 1
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
            existing_versions = current_by_source.get(source_key, [])
            existing = next(
                (
                    record
                    for record in existing_versions
                    if self._equivalent(record, candidate)
                ),
                existing_versions[-1] if existing_versions else None,
            )
            for extra in existing_versions:
                if extra is existing:
                    continue
                self.store.retire(extra.record_id)
                retired += 1
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
                self.store.correct(existing.record_id, replacement)
                current_by_source[source_key] = [replacement]
                updated += 1
            elif self.store.save(candidate):
                current_by_source[source_key] = [candidate]
                inserted += 1
            else:
                duplicates += 1
        if batch.next_cursor is not None:
            for source in sources:
                self.store.set_cursor(
                    self.athlete_id,
                    source,
                    batch.next_cursor,
                    datetime.now(UTC),
                )
        return IngestionResult(inserted, updated, retired, duplicates, raw_files)

    @staticmethod
    def _equivalent(existing: CanonicalRecord, candidate: CanonicalRecord) -> bool:
        def evidence_identity(record: CanonicalRecord) -> tuple[tuple[str, str, str | None], ...]:
            return tuple(
                (item.source, item.source_id, item.raw_hash) for item in record.evidence
            )

        return (
            existing.athlete_id == candidate.athlete_id
            and existing.kind == candidate.kind
            and existing.occurred_at == candidate.occurred_at
            and existing.data == candidate.data
            and existing.quality_flags == candidate.quality_flags
            and existing.schema_version == candidate.schema_version
            and evidence_identity(existing) == evidence_identity(candidate)
        )

    def sync(
        self,
        connector: DataSourceConnector,
        window: SyncWindow,
        *,
        max_attempts: int = 3,
    ) -> IngestionResult:
        source = connector.capabilities().source
        cursor = self.store.get_cursor(self.athlete_id, source)
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            try:
                return self.ingest(connector.sync(window, cursor))
            except (ConnectionError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt + 1 < max_attempts:
                    sleep(0.05 * (2**attempt))
        assert last_error is not None
        raise last_error

    @staticmethod
    def _quality_flags(payload: Mapping[str, JsonValue]) -> tuple[str, ...]:
        non_negative_metrics = {
            "avg_hr",
            "avg_speed_mps",
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
            if (
                key in non_negative_metrics
                and
                isinstance(value, (int, float))
                and value < 0
            ):
                flags.append(f"negative:{key}")
        return tuple(flags)
