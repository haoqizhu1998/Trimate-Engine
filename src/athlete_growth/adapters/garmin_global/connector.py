"""Read-only international Garmin connector using Garmin's official Personal API client."""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import zipfile
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Protocol, cast
from zoneinfo import ZoneInfo

import fitdecode
from garmin_personal_api import GarminPersonalApiClient, HttpError, PersonalApiAuthenticator

from athlete_growth.adapters.garmin_cn.connector import (
    MAX_ORIGINAL_FILE_BYTES,
    _canonical_payload,
    _summary_hash,
)
from athlete_growth.domain.records import JsonValue
from athlete_growth.ports.data_source import (
    HealthStatus,
    RawSourceArtifact,
    RawSourceRecord,
    SourceCapabilities,
    SyncBatch,
    SyncWindow,
)

SOURCE = "garmin-global"
BASE_URL = "https://personal.connectapi.garmin.com"
ACTIVITY_PAGE_SIZE = 100
MAX_ACTIVITY_PAGES = 100


class GarminGlobalGateway(Protocol):
    def verify_key(self, key: str) -> bool: ...

    def fetch(self, start: date, end: date) -> list[dict[str, object]]: ...

    def healthcheck(self) -> bool: ...


class GarminGlobalConnector:
    """Per-user Personal API key, kept apart from the China-region web session."""

    region = "global"
    auth_method = "personal-api-key"

    def __init__(
        self,
        *,
        home: Path,
        timezone_name: str = "UTC",
        gateway: GarminGlobalGateway | None = None,
        known_activity_summaries: Mapping[str, str] | None = None,
    ) -> None:
        self.home = home
        self.timezone = ZoneInfo(timezone_name)
        self.credential_dir = home / "tokens" / "garmin-global-personal-api"
        self.key_file = self.credential_dir / "api-key"
        self.bearer_cache_file = self.credential_dir / "bearer-token.json"
        self._gateway = gateway
        self._known_activity_summaries = dict(known_activity_summaries or {})

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            source=SOURCE,
            region=self.region,
            kinds=(
                "activity", "daily-summary", "sleep", "hrv", "heart-rate",
                "stress", "body-battery", "steps", "body-weight",
            ),
            read_only=True,
            supports_write=False,
            delivery_mode="poll",
            suggested_poll_minutes=30,
        )

    def set_known_activity_summaries(self, summaries: Mapping[str, str]) -> None:
        self._known_activity_summaries = dict(summaries)

    def connect(self, key: str) -> dict[str, object]:
        key = key.strip()
        if not key:
            raise ValueError("Garmin international Personal API key must not be empty")
        gateway = self._gateway or self._personal_gateway()
        try:
            valid = gateway.verify_key(key)
        except Exception as exc:
            raise RuntimeError(
                f"Garmin international key verification failed ({type(exc).__name__})"
            ) from None
        if not valid:
            raise RuntimeError("Garmin international key verification failed")
        self.credential_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.credential_dir.chmod(0o700)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".api-key-", dir=self.credential_dir)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.key_file)
        finally:
            temporary.unlink(missing_ok=True)
        return {"connected": True, "source": SOURCE, "region": self.region}

    def sync(self, window: SyncWindow, cursor: str | None) -> SyncBatch:
        gateway = self._gateway or self._personal_gateway()
        try:
            rows = gateway.fetch(window.start, window.end)
        except HttpError as exc:
            raise RuntimeError(
                f"Garmin international sync failed (HTTP {exc.status_code})"
            ) from None
        return SyncBatch(tuple(self._to_raw(row) for row in rows), window.end.isoformat())

    def healthcheck(self) -> HealthStatus:
        if self._gateway is None and not self.key_file.is_file():
            return HealthStatus(False, "Personal API key is not configured")
        try:
            gateway = self._gateway or self._personal_gateway()
            ok = gateway.healthcheck()
            return HealthStatus(ok, "connected" if ok else "service unavailable")
        except Exception as exc:
            return HealthStatus(False, f"{type(exc).__name__}: connection unavailable")

    def revoke(self) -> None:
        self.key_file.unlink(missing_ok=True)
        self.bearer_cache_file.unlink(missing_ok=True)
        with suppress(OSError):
            self.credential_dir.rmdir()

    def _personal_gateway(self) -> PersonalApiGateway:
        return PersonalApiGateway(
            key_file=self.key_file,
            bearer_cache_file=self.bearer_cache_file,
            timezone=self.timezone,
            known_activity_summaries=self._known_activity_summaries,
        )

    @staticmethod
    def _to_raw(row: dict[str, object]) -> RawSourceRecord:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("Garmin international payload must be an object")
        occurred = datetime.fromisoformat(str(row["occurred_at"]).replace("Z", "+00:00"))
        if occurred.tzinfo is None:
            raise ValueError("Garmin international timestamp must have a timezone")
        kind = str(row["kind"])
        canonical = _canonical_payload(kind, payload)
        if kind == "activity" and isinstance(row.get("calendar_date"), str):
            canonical["calendar_date"] = str(row["calendar_date"])
        original = row.get("original_file")
        artifacts = (
            (RawSourceArtifact("fit-zip", original),)
            if isinstance(original, bytes) and original
            else ()
        )
        return RawSourceRecord(
            source=SOURCE,
            kind=kind,
            source_id=str(row["source_id"]),
            occurred_at=occurred.astimezone(UTC),
            payload=cast(dict[str, JsonValue], payload),
            canonical_payload=canonical,
            artifacts=artifacts,
        )


class PersonalApiGateway:
    """Official client calls with bounded pagination and source-preserving detail."""

    def __init__(
        self,
        *,
        key_file: Path,
        bearer_cache_file: Path,
        timezone: ZoneInfo,
        known_activity_summaries: Mapping[str, str] | None = None,
    ) -> None:
        self.key_file = key_file
        self.bearer_cache_file = bearer_cache_file
        self.timezone = timezone
        self.known_activity_summaries = dict(known_activity_summaries or {})

    def verify_key(self, key: str) -> bool:
        with self._client(key) as client:
            return isinstance(client.get_user_profile_settings(), dict)

    def healthcheck(self) -> bool:
        with self._client(self._read_key()) as client:
            return isinstance(client.get_user_profile_settings(), dict)

    def fetch(self, start: date, end: date) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        with self._client(self._read_key()) as client:
            for summary in self._activities(client, start, end):
                activity_id = summary.get("activityId")
                if activity_id is None:
                    raise ValueError("Garmin international activity lacks a stable ID")
                summary_hash = _summary_hash(summary)
                if self.known_activity_summaries.get(str(activity_id)) == summary_hash:
                    continue
                row = self._activity_row(client, summary, summary_hash, start)
                payload = row.get("payload")
                sync = payload.get("_sync") if isinstance(payload, dict) else None
                if (
                    str(activity_id) in self.known_activity_summaries
                    and isinstance(sync, dict)
                    and any(
                        section in sync.get("missing_sections", [])
                        for section in ("activity", "details", "original_fit_zip")
                    )
                ):
                    continue
                rows.append(row)

            start_text, end_text = start.isoformat(), end.isoformat()
            ranges = (
                ("daily-summary", client.get_daily_summaries(start_text, end_text), ("data",)),
                ("steps", client.get_daily_steps(start_text, end_text), ("data",)),
                ("hrv", client.get_daily_hrv_summaries(start_text, end_text), ("hrvSummaries",)),
                ("stress", client.get_daily_stress(start_text, end_text), ("data",)),
                (
                    "body-battery", client.get_daily_body_battery(start_text, end_text),
                    ("data", "reports"),
                ),
                (
                    "body-weight", client.get_weight_measurements(start_text, end_text),
                    ("dateWeightList", "data"),
                ),
            )
            for kind, document, keys in ranges:
                for item in self._items(document, keys):
                    rows.append(self._row(kind, item, start))
            current = start
            while current <= end:
                sleep = client.get_daily_sleep(current)
                for item in self._items(sleep, ()):
                    item.setdefault("calendarDate", current.isoformat())
                    rows.append(self._row("sleep", item, current))
                heart_rate = client.get_daily_heart_rate(current)
                for item in self._items(heart_rate, ()):
                    item.setdefault("calendarDate", current.isoformat())
                    rows.append(self._row("heart-rate", item, current))
                current += timedelta(days=1)
        return rows

    def _read_key(self) -> str:
        if not self.key_file.is_file() or self.key_file.is_symlink():
            raise RuntimeError("Garmin international Personal API key is not configured")
        key = self.key_file.read_text(encoding="utf-8").strip()
        if not key:
            raise RuntimeError("Garmin international Personal API key is empty")
        return key

    def _client(self, key: str) -> GarminPersonalApiClient:
        self.bearer_cache_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.bearer_cache_file.parent.chmod(0o700)
        if self.bearer_cache_file.is_symlink():
            raise RuntimeError("Garmin international bearer cache must not be a symlink")
        with PersonalApiAuthenticator(
            bearer_token_cache_file=self.bearer_cache_file
        ) as authenticator:
            # The official library allows an environment override; this source is
            # always the international account endpoint.
            authenticator.base_url = BASE_URL
            token = authenticator.obtain_token(key)
        client = GarminPersonalApiClient(token.access_token)
        client.base_url = BASE_URL
        return client

    @staticmethod
    def _items(document: object, keys: tuple[str, ...]) -> list[dict[str, object]]:
        if document is None:
            return []
        if isinstance(document, list):
            if not all(isinstance(item, dict) for item in document):
                raise ValueError("Garmin international response contains non-object rows")
            return [dict(item) for item in document]
        if isinstance(document, dict):
            for key in keys:
                if key in document:
                    return PersonalApiGateway._items(document[key], ())
            if not document:
                return []
            return [dict(document)]
        raise ValueError("Garmin international response has an unexpected shape")

    def _activities(
        self, client: GarminPersonalApiClient, start: date, end: date
    ) -> list[dict[str, object]]:
        activities: list[dict[str, object]] = []
        seen: set[str] = set()
        for page in range(MAX_ACTIVITY_PAGES):
            offset = page * ACTIVITY_PAGE_SIZE
            document = client.search_activities(
                start=offset, limit=ACTIVITY_PAGE_SIZE,
                start_date=start.isoformat(), end_date=end.isoformat(),
            )
            rows = self._items(document, ("activities", "data"))
            for row in rows:
                activity_id = row.get("activityId")
                if activity_id is None or str(activity_id) in seen:
                    raise ValueError("Garmin international activity pagination is incomplete")
                seen.add(str(activity_id))
                activities.append(row)
            if len(rows) < ACTIVITY_PAGE_SIZE:
                return activities
        raise RuntimeError("Garmin international activity pagination limit reached")

    def _activity_row(
        self,
        client: GarminPersonalApiClient,
        summary: dict[str, object],
        summary_hash: str,
        fallback_date: date,
    ) -> dict[str, object]:
        activity_id = str(summary["activityId"])
        row = self._row("activity", summary, fallback_date)
        sections: dict[str, object] = {}
        missing: list[str] = []
        try:
            detail = client.get_activity(activity_id)
            if isinstance(detail, dict):
                sections["activity"] = detail
            else:
                missing.append("activity")
        except Exception:
            missing.append("activity")
        try:
            workouts = client.get_activity_workouts(activity_id)
            if workouts is not None:
                sections["workout_steps"] = workouts
        except Exception:
            pass
        original: bytes | None = None
        parsed: dict[str, object] | None = None
        try:
            candidate = client.download_activity(activity_id)
            if (
                isinstance(candidate, bytes)
                and 0 < len(candidate) <= MAX_ORIGINAL_FILE_BYTES
                and zipfile.is_zipfile(io.BytesIO(candidate))
            ):
                original = candidate
                parsed = _fit_summary(candidate)
        except Exception:
            pass
        if original is None:
            missing.append("original_fit_zip")
        if parsed is None:
            missing.append("details")
        else:
            sections["details"] = {"source": "original_fit_zip"}
            sections["laps"] = {"lapDTOs": parsed["laps"]}
        payload = dict(summary)
        payload["_activity_sections"] = sections
        payload["_sync"] = {
            "summary_sha256": summary_hash,
            "missing_sections": missing,
            "original_fit_zip_sha256": hashlib.sha256(original).hexdigest() if original else None,
            "recorded_sample_count": parsed["recorded_sample_count"] if parsed else 0,
        }
        row["payload"] = payload
        if original is not None:
            row["original_file"] = original
        return row

    def _row(self, kind: str, item: dict[str, object], fallback_date: date) -> dict[str, object]:
        identity_keys = (
            ("activityId",) if kind == "activity"
            else ("samplePk", "timestampGMT", "calendarDate", "date") if kind == "body-weight"
            else ("calendarDate", "date", "summaryId", "id")
        )
        source_id = next(
            (item[key] for key in identity_keys if item.get(key) is not None),
            f"{fallback_date.isoformat()}:{kind}:0",
        )
        date_key, date_value = next(
            (
                (key, item[key]) for key in (
                    "startTimeGMT", "startTimeLocal", "timestampGMT",
                    "calendarDate", "date", "startDate"
                ) if item.get(key) is not None
            ),
            ("calendarDate", fallback_date.isoformat()),
        )
        occurred = self._parse_datetime(date_key, date_value)
        row: dict[str, object] = {
            "kind": kind, "source_id": str(source_id),
            "occurred_at": occurred.isoformat(), "payload": item,
        }
        if kind == "activity":
            local = item.get("startTimeLocal")
            if isinstance(local, str):
                row["calendar_date"] = (
                    self._parse_datetime("startTimeLocal", local).date().isoformat()
                )
            else:
                row["calendar_date"] = occurred.astimezone(self.timezone).date().isoformat()
        return row

    def _parse_datetime(self, field: str, value: object) -> datetime:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=UTC)
        text = str(value).strip().replace(" ", "T").replace("Z", "+00:00")
        if "T" not in text:
            return datetime.combine(date.fromisoformat(text[:10]), time.min, tzinfo=self.timezone)
        occurred = datetime.fromisoformat(text)
        if occurred.tzinfo is None:
            return occurred.replace(tzinfo=UTC if field.endswith("GMT") else self.timezone)
        return occurred


def _fit_summary(content: bytes) -> dict[str, object] | None:
    """Read lap and sample counts without expanding the original ZIP to disk."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            fit_files = [
                item for item in archive.infolist() if item.filename.lower().endswith(".fit")
            ]
            if not fit_files or any(item.file_size > MAX_ORIGINAL_FILE_BYTES for item in fit_files):
                return None
            with archive.open(fit_files[0]) as stream:
                fit_bytes = stream.read(MAX_ORIGINAL_FILE_BYTES + 1)
        if len(fit_bytes) > MAX_ORIGINAL_FILE_BYTES:
            return None
        laps: list[dict[str, object]] = []
        sample_count = 0
        with fitdecode.FitReader(io.BytesIO(fit_bytes)) as reader:
            for frame in reader:
                if not isinstance(frame, fitdecode.FitDataMessage):
                    continue
                if frame.name == "record":
                    sample_count += 1
                elif frame.name == "lap" and len(laps) < 200:
                    fields = {field.name: field.value for field in frame.fields}
                    laps.append({
                        "duration": fields.get("total_timer_time"),
                        "distance": fields.get("total_distance"),
                        "averageHR": fields.get("avg_heart_rate"),
                        "averagePower": fields.get("avg_power"),
                    })
        return {"laps": laps, "recorded_sample_count": sample_count}
    except (OSError, ValueError, zipfile.BadZipFile, fitdecode.FitError):
        return None
