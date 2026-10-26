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
    duplicates: int
    raw_files: int


class IngestionService:
    def __init__(self, *, athlete_id: str, store: SQLiteRecordStore, raw_dir: Path) -> None:
        self.athlete_id = athlete_id
        self.store = store
        self.raw_dir = raw_dir

    def ingest(self, batch: SyncBatch) -> IngestionResult:
        inserted = 0
        duplicates = 0
        raw_files = 0
        sources: set[str] = set()
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
            flags = self._quality_flags(raw.payload)
            record = CanonicalRecord.create(
                athlete_id=self.athlete_id,
                kind=raw.kind,
                occurred_at=raw.occurred_at,
                data=raw.payload,
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
            if self.store.save(record):
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
        return IngestionResult(inserted, duplicates, raw_files)

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
        flags: list[str] = []
        for key, value in payload.items():
            if (
                isinstance(value, (int, float))
                and value < 0
                and key
                not in {
                    "temperature_c",
                    "elevation_m",
                }
            ):
                flags.append(f"negative:{key}")
        return tuple(flags)
