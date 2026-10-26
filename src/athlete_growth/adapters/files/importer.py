"""CSV, JSON, TCX and FIT data-source adapter."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

from athlete_growth.domain.records import JsonValue
from athlete_growth.ports.data_source import RawSourceRecord, SyncBatch


class FileDataSource:
    """Read user-controlled offline exports into raw source records."""

    def read(self, path: Path) -> SyncBatch:
        suffix = path.suffix.lower()
        if suffix == ".csv":
            records = self._read_csv(path)
        elif suffix == ".tcx":
            records = self._read_tcx(path)
        elif suffix == ".json":
            records = self._read_json(path)
        elif suffix == ".fit":
            records = self._read_fit(path)
        else:
            raise ValueError(f"unsupported import format: {suffix}")
        return SyncBatch(tuple(records), next_cursor=path.name)

    def _read_csv(self, path: Path) -> list[RawSourceRecord]:
        records: list[RawSourceRecord] = []
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row_number, row in enumerate(csv.DictReader(handle), start=2):
                occurred = self._parse_datetime(row.pop("occurred_at"))
                kind = row.pop("kind", "activity") or "activity"
                source_id = row.pop("source_id", "") or f"{path.name}:{row_number}"
                payload = {
                    key: self._typed(value) for key, value in row.items() if value not in (None, "")
                }
                records.append(
                    RawSourceRecord(
                        source="file",
                        kind=kind,
                        source_id=source_id,
                        occurred_at=occurred,
                        payload=payload,
                    )
                )
        return records

    def _read_tcx(self, path: Path) -> list[RawSourceRecord]:
        root = ElementTree.parse(path).getroot()
        records: list[RawSourceRecord] = []
        for index, activity in enumerate(root.findall(".//{*}Activity"), start=1):
            identifier = activity.findtext("{*}Id")
            if not identifier:
                raise ValueError("TCX activity is missing Id")
            laps = activity.findall("{*}Lap")
            duration_seconds = sum(float(lap.findtext("{*}TotalTimeSeconds") or 0) for lap in laps)
            distance_m = sum(float(lap.findtext("{*}DistanceMeters") or 0) for lap in laps)
            heart_rates = [
                float(value)
                for value in (lap.findtext("{*}AverageHeartRateBpm/{*}Value") for lap in laps)
                if value
            ]
            payload: dict[str, JsonValue] = {
                "sport": (activity.attrib.get("Sport") or "other").lower(),
                "duration_min": duration_seconds / 60.0,
                "distance_m": distance_m,
            }
            if heart_rates:
                payload["avg_hr"] = sum(heart_rates) / len(heart_rates)
            records.append(
                RawSourceRecord(
                    source="file",
                    kind="activity",
                    source_id=f"{path.name}:{identifier}:{index}",
                    occurred_at=self._parse_datetime(identifier),
                    payload=payload,
                )
            )
        return records

    def _read_json(self, path: Path) -> list[RawSourceRecord]:
        document = json.loads(path.read_text(encoding="utf-8"))
        rows = document if isinstance(document, list) else document.get("records", [])
        records: list[RawSourceRecord] = []
        for index, row in enumerate(rows, start=1):
            payload = row.get("payload") or {
                key: value
                for key, value in row.items()
                if key not in {"kind", "source_id", "occurred_at"}
            }
            records.append(
                RawSourceRecord(
                    source="file",
                    kind=row.get("kind", "observation"),
                    source_id=row.get("source_id", f"{path.name}:{index}"),
                    occurred_at=self._parse_datetime(row["occurred_at"]),
                    payload=payload,
                )
            )
        return records

    def _read_fit(self, path: Path) -> list[RawSourceRecord]:
        try:
            import fitdecode
        except ImportError as exc:
            raise RuntimeError("FIT import requires the fitdecode package") from exc
        session: dict[str, object] = {}
        with fitdecode.FitReader(path) as reader:
            for frame in reader:
                if isinstance(frame, fitdecode.FitDataMessage) and frame.name == "session":
                    session = {field.name: field.value for field in frame.fields}
                    break
        start = session.get("start_time") or session.get("timestamp")
        if not isinstance(start, datetime):
            start = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        payload: dict[str, JsonValue] = {
            "sport": str(session.get("sport") or "other"),
            "duration_min": float(str(session.get("total_timer_time") or 0)) / 60.0,
            "distance_m": float(str(session.get("total_distance") or 0)),
        }
        if session.get("avg_heart_rate") is not None:
            payload["avg_hr"] = float(str(session["avg_heart_rate"]))
        return [
            RawSourceRecord(
                source="file",
                kind="activity",
                source_id=path.name,
                occurred_at=start,
                payload=payload,
            )
        ]

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("imported occurred_at must include a timezone")
        return parsed

    @staticmethod
    def _typed(value: str) -> JsonValue:
        lowered = value.lower()
        if lowered in {"true", "false"}:
            return lowered == "true"
        try:
            return float(value)
        except ValueError:
            return value
