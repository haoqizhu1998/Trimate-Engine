"""Canonical data quality inspection."""

from __future__ import annotations

from dataclasses import dataclass

from athlete_growth.domain.records import CanonicalRecord


@dataclass(frozen=True, slots=True)
class DataQualityFinding:
    severity: str
    code: str
    message: str
    record_ids: tuple[str, ...]


def inspect_records(records: list[CanonicalRecord]) -> list[DataQualityFinding]:
    findings: list[DataQualityFinding] = []
    by_moment: dict[tuple[str, str], list[CanonicalRecord]] = {}
    devices_by_kind: dict[str, dict[str, list[str]]] = {}
    for record in records:
        if not record.evidence and record.kind not in {
            "check-in",
            "context-event",
            "goal",
            "training-plan",
        }:
            findings.append(
                DataQualityFinding(
                    "warning",
                    "missing-provenance",
                    f"{record.kind} has no source evidence",
                    (record.record_id,),
                )
            )
        for flag in record.quality_flags:
            findings.append(
                DataQualityFinding("warning", "source-quality-flag", flag, (record.record_id,))
            )
        by_moment.setdefault((record.kind, record.occurred_at.isoformat()), []).append(record)
        device = record.data.get("device_id") or record.data.get("deviceId")
        if isinstance(device, (str, int)):
            devices_by_kind.setdefault(record.kind, {}).setdefault(str(device), []).append(
                record.record_id
            )
    for group in by_moment.values():
        payloads = {repr(sorted(record.data.items())) for record in group}
        sources = {evidence.source for record in group for evidence in record.evidence}
        if len(payloads) > 1 and len(sources) > 1:
            findings.append(
                DataQualityFinding(
                    "warning",
                    "source-conflict",
                    "different sources disagree for the same record time",
                    tuple(record.record_id for record in group),
                )
            )
    for kind, devices in devices_by_kind.items():
        if len(devices) > 1:
            findings.append(
                DataQualityFinding(
                    "info",
                    "device-change",
                    f"{kind} records contain multiple device identifiers: "
                    + ", ".join(sorted(devices)),
                    tuple(record_id for ids in devices.values() for record_id in ids),
                )
            )
    return findings
