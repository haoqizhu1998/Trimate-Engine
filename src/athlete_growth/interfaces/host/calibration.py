"""Host-neutral sport calibration entry points."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from athlete_growth.application.calibration import SportCalibrationService
from athlete_growth.interfaces.host.tools import HostRuntime


def _service(data_home: str | Path) -> SportCalibrationService:
    runtime = HostRuntime.load(data_home)
    return SportCalibrationService(runtime.athlete_id, runtime.records)


def get_sport_calibration(data_home: str | Path, sport: str) -> dict[str, object]:
    return _service(data_home).get(sport)


def preview_sport_calibration(
    data_home: str | Path,
    sport: str,
    value: float,
    tested_on: date,
    protocol: str,
    source_activity_id: str | None = None,
) -> dict[str, object]:
    return _service(data_home).preview(sport, value, tested_on, protocol, source_activity_id)


def confirm_sport_calibration(
    data_home: str | Path,
    sport: str,
    value: float,
    tested_on: date,
    protocol: str,
    preview_hash: str,
    source_activity_id: str | None = None,
) -> dict[str, object]:
    return _service(data_home).confirm(
        sport, value, tested_on, protocol, preview_hash, source_activity_id
    )
