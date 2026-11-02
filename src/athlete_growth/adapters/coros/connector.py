"""Personal COROS MCP activity source, using the user's official local OAuth helper."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol, cast

from athlete_growth.domain.records import JsonValue
from athlete_growth.ports.data_source import (
    HealthStatus,
    RawSourceRecord,
    SourceCapabilities,
    SyncBatch,
    SyncWindow,
)

CN_TIMEZONE = timezone(timedelta(hours=8))
LIST_LIMIT = 100
HELPER_NAME = "coros_mcp_login.py"
HELPER_URL = (
    "https://raw.githubusercontent.com/coroslab/COROS-MCP/main/"
    "skill/coros_mcp_login_gateway/scripts/coros_mcp_login.py"
)


def install_official_helper(home: Path) -> Path:
    """Download the unmodified official helper into the private runtime directory."""
    target = home / "integrations" / HELPER_NAME
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    with urllib.request.urlopen(HELPER_URL, timeout=30) as response:  # noqa: S310
        content = response.read(512_001)
    if (
        len(content) > 512_000
        or not content.startswith(b"#!/usr/bin/env python3")
        or b"login-start" not in content
        or b"call-tool" not in content
    ):
        raise ValueError("official COROS helper download has unexpected content")
    temporary = target.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    return target


class CorosGateway(Protocol):
    def call(self, tool: str, arguments: dict[str, object]) -> object: ...


class CorosHelperGateway:
    """Invoke COROS's own OAuth/MCP helper without handling tokens in this product."""

    def __init__(self, *, home: Path, helper_path: Path | None = None) -> None:
        configured = os.environ.get("ATHLETE_GROWTH_COROS_HELPER")
        self.helper_path = (
            helper_path
            if helper_path is not None else
            Path(configured).expanduser() if configured else
            home / "integrations" / HELPER_NAME
        )

    def command(self, action: str, *arguments: str) -> object:
        if not self.helper_path.is_file():
            raise FileNotFoundError(
                "COROS helper is not installed in the private data home; "
                "see docs/device-connections.md"
            )
        if action not in {
            "login-start", "login-finish", "login-status", "logout", "call-tool"
        }:
            raise ValueError("unsupported COROS helper action")
        result = subprocess.run(
            [sys.executable, str(self.helper_path), "--issuer", "https://mcpcn.coros.com",
             action, *arguments],
            capture_output=True,
            text=True,
            timeout=360 if action == "login-finish" else 120,
            check=False,
        )
        if result.returncode:
            # The helper may print an account URL or diagnostic. Never put either in logs.
            raise RuntimeError(f"COROS helper {action} failed; run it locally for details")
        if action != "call-tool":
            return result.stdout.strip()
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("COROS helper did not return JSON") from exc

    def call(self, tool: str, arguments: dict[str, object]) -> object:
        return self.command(
            "call-tool", "--tool", tool, "--arguments-json",
            json.dumps(arguments, separators=(",", ":")),
        )


class CorosConnector:
    def __init__(self, *, home: Path, gateway: CorosGateway | None = None) -> None:
        self.gateway = gateway or CorosHelperGateway(home=home)

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            source="coros", region="cn", kinds=("activity",),
            delivery_mode="poll", suggested_poll_minutes=30,
        )

    def sync(self, window: SyncWindow, cursor: str | None) -> SyncBatch:
        # COROS list has a return limit and no documented pagination. Query a day at
        # a time and reject a full page rather than silently dropping activities.
        records: list[RawSourceRecord] = []
        day = window.start
        while day <= window.end:
            payload = _unwrap(self.gateway.call("querySportRecords", {
                "startDate": day.strftime("%Y%m%d"),
                "endDate": day.strftime("%Y%m%d"),
                "limit": LIST_LIMIT,
            }))
            summaries = _records(payload)
            if len(summaries) >= LIST_LIMIT:
                raise ValueError("COROS activity list reached its limit; narrow the sync window")
            for summary in summaries:
                label_id = _required_text(summary, "labelId", "label_id")
                sport_type = _required_int(summary, "sportType", "sportTypeCode")
                args = {"labelId": label_id, "sportType": sport_type}
                detail = _unwrap(self.gateway.call("getActivityDetail", args))
                laps = _unwrap(self.gateway.call("queryActivityLapData", args))
                occurred = _occurred_at(summary, detail)
                if occurred.astimezone(CN_TIMEZONE).date() != day:
                    raise ValueError("COROS activity date does not match query day")
                raw: dict[str, JsonValue] = cast(dict[str, JsonValue], {
                    "summary": summary, "detail": detail, "laps": laps,
                    "_sync": {"detail_status": "partial", "missing_sections": ["original_fit"]},
                })
                records.append(RawSourceRecord(
                    source="coros", kind="activity", source_id=label_id,
                    occurred_at=occurred, payload=raw,
                    canonical_payload=_canonical(summary, detail, laps, occurred, sport_type),
                ))
            day += timedelta(days=1)
        return SyncBatch(tuple(records), next_cursor=window.end.isoformat())

    def healthcheck(self) -> HealthStatus:
        try:
            _unwrap(self.gateway.call("queryUserInfo", {}))
        except (OSError, RuntimeError, ValueError, TimeoutError):
            return HealthStatus(False, "COROS local OAuth connection unavailable")
        return HealthStatus(True, "COROS local OAuth connection available")

    def revoke(self) -> None:
        if not isinstance(self.gateway, CorosHelperGateway):
            raise RuntimeError("COROS test gateway cannot revoke OAuth")
        self.gateway.command("logout")


def _unwrap(result: object) -> object:
    if not isinstance(result, dict):
        raise ValueError("COROS MCP returned an unsupported result")
    if result.get("isError") is True:
        raise RuntimeError("COROS MCP tool reported an error")
    structured = result.get("structuredContent")
    if isinstance(structured, (dict, list)):
        return structured
    content = result.get("content")
    if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict):
        text = content[0].get("text")
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "COROS MCP returned prose without machine-readable activity IDs; "
                    "automatic import cannot safely continue"
                ) from exc
    raise ValueError("COROS MCP returned no machine-readable data")


def _records(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        for key in ("records", "activities", "sportRecords", "data"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                value = candidate
                break
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("COROS activity list has an unsupported schema")
    return cast(list[dict[str, object]], value)


def _value(data: object, *names: str) -> object:
    if not isinstance(data, dict):
        return None
    for name in names:
        if data.get(name) is not None:
            return data[name]
    return None


def _required_text(data: object, *names: str) -> str:
    value = _value(data, *names)
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value)
    raise ValueError("COROS activity is missing its stable identifier")


def _required_int(data: object, *names: str) -> int:
    value = _value(data, *names)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    raise ValueError("COROS activity is missing its sport code")


def _occurred_at(summary: object, detail: object) -> datetime:
    value = _value(summary, "startTime", "startTimeLocal", "startTimestamp", "startTimeGMT")
    if value is None:
        value = _value(detail, "startTime", "startTimeLocal", "startTimestamp", "startTimeGMT")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000 if value > 10_000_000_000 else value, UTC)
    if isinstance(value, str):
        try:
            text = value.strip().replace(" ", "T").replace("Z", "+00:00")
            if "T" not in text:
                raise ValueError("date-only start time")
            parsed = datetime.fromisoformat(text)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=CN_TIMEZONE)
        except ValueError:
            pass
    raise ValueError("COROS activity is missing a precise start time")


def _number(data: object, *names: str) -> float | None:
    value = _value(data, *names)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _canonical(
    summary: dict[str, object], detail: object, laps: object,
    occurred: datetime, sport_type: int,
) -> dict[str, JsonValue]:
    duration = _number(detail, "durationSeconds", "duration", "workoutTimeSeconds")
    if duration is None:
        duration = _number(summary, "durationSeconds", "duration")
    distance = _number(detail, "distanceMeters", "distanceM")
    if distance is None:
        distance = _number(summary, "distanceMeters", "distanceM")
    distance_km = _number(detail, "distanceKm") or _number(summary, "distanceKm")
    if distance is None and distance_km is not None:
        distance = distance_km * 1000
    groups = _value(laps, "lapGroups")
    lap_rows = (
        [
            item
            for group in groups if isinstance(group, dict)
            for item in group.get("laps", []) if isinstance(item, dict)
        ]
        if isinstance(groups, list) else []
    )
    return cast(dict[str, JsonValue], {
        "calendar_date": occurred.astimezone(CN_TIMEZONE).date().isoformat(),
        "sport": _value(summary, "sportName", "sport") or f"coros:{sport_type}",
        "coros_sport_code": sport_type,
        "duration_min": duration / 60 if duration is not None else None,
        "distance_m": distance,
        "avg_hr": _number(detail, "avgHr", "averageHeartRate"),
        "max_hr": _number(detail, "maxHr", "maximumHeartRate"),
        "avg_power_w": _number(detail, "avgPower", "averagePower"),
        "avg_cadence_rpm": _number(detail, "avgCadence", "averageCadence"),
        "lap_count": len(lap_rows),
        "laps": lap_rows[:200],
        "detail_status": "partial",
        "detail_missing_sections": ["original_fit"],
    })
