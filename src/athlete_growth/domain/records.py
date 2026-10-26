"""Versioned canonical records and evidence references."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


def _aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    source: str
    source_id: str
    captured_at: datetime
    raw_hash: str | None = None

    def __post_init__(self) -> None:
        _aware(self.captured_at, "captured_at")
        if not self.source.strip() or not self.source_id.strip():
            raise ValueError("evidence source and source_id must not be blank")
        if self.raw_hash is not None and (
            len(self.raw_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.raw_hash)
        ):
            raise ValueError("raw_hash must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class CanonicalRecord:
    record_id: str
    athlete_id: str
    kind: str
    occurred_at: datetime
    data: dict[str, JsonValue]
    evidence: tuple[EvidenceRef, ...] = ()
    quality_flags: tuple[str, ...] = ()
    schema_version: int = 1
    supersedes_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        _aware(self.occurred_at, "occurred_at")
        _aware(self.created_at, "created_at")
        if not self.athlete_id.strip() or not self.kind.strip():
            raise ValueError("athlete_id and kind must not be blank")
        if self.schema_version < 1:
            raise ValueError("schema_version must be positive")

    @classmethod
    def create(
        cls,
        *,
        athlete_id: str,
        kind: str,
        occurred_at: datetime,
        data: dict[str, JsonValue],
        evidence: tuple[EvidenceRef, ...] = (),
        quality_flags: tuple[str, ...] = (),
        schema_version: int = 1,
        supersedes_id: str | None = None,
        created_at: datetime | None = None,
    ) -> CanonicalRecord:
        _aware(occurred_at, "occurred_at")
        identity = {
            "athlete_id": athlete_id,
            "kind": kind,
            "occurred_at": occurred_at.isoformat(),
            "data": data,
            "evidence": [
                {
                    "source": item.source,
                    "source_id": item.source_id,
                    "raw_hash": item.raw_hash,
                }
                for item in evidence
            ],
            "schema_version": schema_version,
            "supersedes_id": supersedes_id,
        }
        serialized = json.dumps(
            identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return cls(
            record_id=hashlib.sha256(serialized).hexdigest(),
            athlete_id=athlete_id,
            kind=kind,
            occurred_at=occurred_at,
            data=dict(data),
            evidence=tuple(evidence),
            quality_flags=tuple(quality_flags),
            schema_version=schema_version,
            supersedes_id=supersedes_id,
            created_at=created_at or datetime.now(UTC),
        )
