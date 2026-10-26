"""User-provided race CSV and JSON connector."""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path

from athlete_growth.domain.race import RaceEvent


class RaceFileConnector:
    def read(self, path: Path) -> list[RaceEvent]:
        if path.suffix.lower() == ".json":
            document = json.loads(path.read_text(encoding="utf-8"))
            rows = document if isinstance(document, list) else [document]
        elif path.suffix.lower() == ".csv":
            with path.open(newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
        else:
            raise ValueError("race import supports JSON and CSV")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError("race import must contain an object or list of objects")
        return [self._event(row) for row in rows]

    @staticmethod
    def _event(row: Mapping[str, object]) -> RaceEvent:
        return RaceEvent(
            event_id=str(row["event_id"]),
            name=str(row["name"]),
            event_date=date.fromisoformat(str(row["event_date"])),
            distance_km=float(str(row["distance_km"])),
            elevation_gain_m=float(str(row.get("elevation_gain_m", 0))),
            location=str(row.get("location", "")),
            source_url=str(row["source_url"]),
            source_updated_at=datetime.fromisoformat(
                str(row["source_updated_at"]).replace("Z", "+00:00")
            ),
            license=str(row["license"]),
        )
