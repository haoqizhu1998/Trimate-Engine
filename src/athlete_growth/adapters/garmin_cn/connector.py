"""Read-only Garmin Connect China session connector."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Protocol, cast

from athlete_growth.domain.records import CanonicalRecord, JsonValue
from athlete_growth.ports.data_source import (
    HealthStatus,
    RawSourceArtifact,
    RawSourceRecord,
    SourceCapabilities,
    SyncBatch,
    SyncWindow,
)

CN_TIMEZONE = timezone(timedelta(hours=8))
MAX_ORIGINAL_FILE_BYTES = 128 * 1024 * 1024


class GarminGateway(Protocol):
    def authenticate(
        self, email: str, password: str, prompt_mfa: Callable[[], str]
    ) -> dict[str, object]: ...

    def fetch(self, start: date, end: date) -> list[dict[str, object]]: ...

    def healthcheck(self) -> bool: ...


class GarminSessionClient(Protocol):
    display_name: str | None

    def login(self, tokenstore: str) -> tuple[object, object]: ...

    def get_userprofile_settings(self) -> dict[str, object]: ...

    def get_activities_by_date(
        self, startdate: str, enddate: str
    ) -> list[dict[str, object]]: ...

    def get_activity(self, activity_id: str) -> dict[str, object]: ...

    def get_activity_details(self, activity_id: str) -> dict[str, object]: ...

    def get_activity_laps(self, activity_id: str) -> dict[str, object]: ...

    def get_activity_splits(self, activity_id: str) -> dict[str, object]: ...

    def get_activity_hr_in_timezones(self, activity_id: str) -> dict[str, object]: ...

    def get_activity_power_in_timezones(self, activity_id: str) -> dict[str, object]: ...

    def download_activity(self, activity_id: str, dl_fmt: object) -> bytes: ...

    def get_daily_summaries(self, start: str, end: str) -> dict[str, object]: ...

    def get_daily_steps(self, start: str, end: str) -> list[dict[str, object]]: ...

    def get_sleep_daily(self, start: str, end: str) -> list[dict[str, object]]: ...

    def get_hrv_data_range(self, start: str, end: str) -> dict[str, object] | None: ...

    def get_daily_heart_rate_stats(
        self, start: str, end: str
    ) -> list[dict[str, object]]: ...

    def get_daily_stress_stats(
        self, start: str, end: str
    ) -> list[dict[str, object]]: ...

    def get_daily_body_battery_stats(
        self, start: str, end: str
    ) -> list[dict[str, object]]: ...

    def get_weight_snapshot(self, start: str, end: str) -> dict[str, object]: ...


GarminClientFactory = Callable[..., GarminSessionClient]


class GarminCnConnector:
    """Primary China-region connector backed by a local Garmin web session."""

    region = "cn"
    auth_method = "session"

    def __init__(
        self,
        *,
        home: Path,
        gateway: GarminGateway | None = None,
        known_activity_summaries: Mapping[str, str] | None = None,
    ) -> None:
        self.home = home
        self.session_token_dir = home / "tokens" / "garmin-cn-session"
        self.session_token_file = self.session_token_dir / "garmin_tokens.json"
        self._gateway = gateway
        self._known_activity_summaries = dict(known_activity_summaries or {})

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            source="garmin-cn",
            region="cn",
            kinds=(
                "activity",
                "daily-summary",
                "sleep",
                "hrv",
                "heart-rate",
                "stress",
                "body-battery",
                "steps",
                "body-weight",
            ),
            read_only=True,
            supports_write=False,
            delivery_mode="poll",
            suggested_poll_minutes=30,
        )

    def set_known_activity_summaries(self, summaries: Mapping[str, str]) -> None:
        self._known_activity_summaries = dict(summaries)

    def authenticate(
        self, *, email: str, password: str, prompt_mfa: Callable[[], str]
    ) -> dict[str, object]:
        gateway = self._gateway or GarminCnSessionGateway(token_dir=self.session_token_dir)
        return gateway.authenticate(email, password, prompt_mfa)

    def sync(self, window: SyncWindow, cursor: str | None) -> SyncBatch:
        gateway = self._gateway or GarminCnSessionGateway(
            token_dir=self.session_token_dir,
            known_activity_summaries=self._known_activity_summaries,
        )
        rows = gateway.fetch(window.start, window.end)
        records = tuple(self._to_raw(row) for row in rows)
        return SyncBatch(records, next_cursor=window.end.isoformat())

    def healthcheck(self) -> HealthStatus:
        try:
            gateway = self._gateway or GarminCnSessionGateway(
                token_dir=self.session_token_dir
            )
            ok = gateway.healthcheck()
            return HealthStatus(ok, "connected" if ok else "service unavailable")
        except Exception as exc:
            return HealthStatus(False, f"{type(exc).__name__}: {exc}")

    def revoke(self) -> None:
        with suppress(FileNotFoundError):
            self.session_token_file.unlink()
        with suppress(FileNotFoundError, OSError):
            self.session_token_dir.rmdir()

    @staticmethod
    def _to_raw(row: dict[str, object]) -> RawSourceRecord:
        occurred = datetime.fromisoformat(str(row["occurred_at"]).replace("Z", "+00:00"))
        payload = row.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("Garmin gateway payload must be an object")
        kind = str(row["kind"])
        artifact_bytes = row.get("original_file")
        artifacts = (
            (RawSourceArtifact("fit-zip", artifact_bytes),)
            if isinstance(artifact_bytes, bytes) and artifact_bytes
            else ()
        )
        canonical_payload = _canonical_payload(kind, payload)
        if kind == "activity":
            calendar_date = row.get("calendar_date")
            if isinstance(calendar_date, str):
                try:
                    activity_date = date.fromisoformat(calendar_date)
                except ValueError:
                    activity_date = occurred.date()
            else:
                activity_date = occurred.date()
            canonical_payload["calendar_date"] = activity_date.isoformat()
        return RawSourceRecord(
            source="garmin-cn",
            kind=kind,
            source_id=str(row["source_id"]),
            occurred_at=occurred.astimezone(UTC),
            payload=cast(dict[str, JsonValue], payload),
            canonical_payload=canonical_payload,
            artifacts=artifacts,
        )


class GarminCnSessionGateway:
    """Thin read-only composition around the open-source ``garminconnect`` client."""

    def __init__(
        self,
        *,
        token_dir: Path,
        client_factory: GarminClientFactory | None = None,
        known_activity_summaries: Mapping[str, str] | None = None,
    ) -> None:
        self.token_dir = token_dir
        self._client_factory = client_factory
        self._known_activity_summaries = dict(known_activity_summaries or {})

    def authenticate(
        self, email: str, password: str, prompt_mfa: Callable[[], str]
    ) -> dict[str, object]:
        self.token_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.token_dir.chmod(0o700)
        client = self._factory()(
            email=email,
            password=password,
            is_cn=True,
            prompt_mfa=prompt_mfa,
        )
        client.login(str(self.token_dir))
        return {"display_name": client.display_name, "region": "cn"}

    def fetch(self, start: date, end: date) -> list[dict[str, object]]:
        client = self._cached_client()
        rows: list[dict[str, object]] = []
        start_text = start.isoformat()
        end_text = end.isoformat()

        for index, activity in enumerate(
            client.get_activities_by_date(start_text, end_text)
        ):
            activity_id = activity.get("activityId")
            if activity_id is None:
                rows.append(self._row("activity", activity, start, index))
                continue
            summary_hash = _summary_hash(activity)
            if self._known_activity_summaries.get(str(activity_id)) == summary_hash:
                continue
            activity_row = self._activity_row(client, activity, start, index, summary_hash)
            payload = activity_row["payload"]
            sync_metadata = payload.get("_sync") if isinstance(payload, dict) else None
            missing_sections = (
                sync_metadata.get("missing_sections", [])
                if isinstance(sync_metadata, dict)
                else []
            )
            if (
                str(activity_id) in self._known_activity_summaries
                and isinstance(missing_sections, list)
                and any(
                    name in missing_sections
                    for name in ("activity", "details", "original_fit_zip")
                )
            ):
                # A transient endpoint failure must not replace a complete version.
                continue
            rows.append(activity_row)

        range_documents: tuple[tuple[str, object, str | None], ...] = (
            (
                "daily-summary",
                client.get_daily_summaries(start_text, end_text),
                "data",
            ),
            ("steps", client.get_daily_steps(start_text, end_text), None),
            ("sleep", client.get_sleep_daily(start_text, end_text), None),
            ("hrv", client.get_hrv_data_range(start_text, end_text), "hrvSummaries"),
            (
                "heart-rate",
                client.get_daily_heart_rate_stats(start_text, end_text),
                None,
            ),
            ("stress", client.get_daily_stress_stats(start_text, end_text), None),
            (
                "body-battery",
                client.get_daily_body_battery_stats(start_text, end_text),
                None,
            ),
            (
                "body-weight",
                client.get_weight_snapshot(start_text, end_text),
                "dateWeightList",
            ),
        )
        for kind, document, collection_key in range_documents:
            rows.extend(
                self._document_rows(
                    kind,
                    self._collection(document, collection_key),
                    start,
                )
            )
        return rows

    @classmethod
    def _activity_row(
        cls,
        client: GarminSessionClient,
        summary: dict[str, object],
        fallback_date: date,
        index: int,
        summary_hash: str,
    ) -> dict[str, object]:
        activity_id = str(summary["activityId"])
        row = cls._row("activity", summary, fallback_date, index)
        sections: dict[str, object] = {}
        missing: list[str] = []
        methods = {
            "activity": "get_activity",
            "details": "get_activity_details",
            "laps": "get_activity_laps",
            "splits": "get_activity_splits",
            "split_summaries": "get_activity_split_summaries",
            "typed_splits": "get_activity_typed_splits",
            "hr_zones": "get_activity_hr_in_timezones",
            "power_zones": "get_activity_power_in_timezones",
            "workout_steps": "get_activity_step_details",
        }
        activity_type = summary.get("activityType")
        type_key = activity_type.get("typeKey") if isinstance(activity_type, dict) else None
        if isinstance(type_key, str) and "swim" in type_key:
            methods["swim_details"] = "get_activity_swim_details"
        for section, method_name in methods.items():
            method = getattr(client, method_name, None)
            if not callable(method):
                missing.append(section)
                continue
            try:
                document = method(activity_id)
            except Exception:
                missing.append(section)
                continue
            if document is None:
                missing.append(section)
            else:
                sections[section] = document

        original: bytes | None = None
        try:
            from garminconnect import Garmin

            original = client.download_activity(
                activity_id, Garmin.ActivityDownloadFormat.ORIGINAL
            )
        except Exception:
            missing.append("original_fit_zip")
        if (
            not isinstance(original, bytes)
            or not original
            or len(original) > MAX_ORIGINAL_FILE_BYTES
            or not zipfile.is_zipfile(io.BytesIO(original))
        ):
            original = None
            if "original_fit_zip" not in missing:
                missing.append("original_fit_zip")

        payload = dict(summary)
        payload["_activity_sections"] = sections
        payload["_sync"] = {
            "summary_sha256": summary_hash,
            "missing_sections": missing,
            "original_fit_zip_sha256": (
                hashlib.sha256(original).hexdigest() if original else None
            ),
        }
        row["payload"] = payload
        if original is not None:
            row["original_file"] = original
        return row

    def healthcheck(self) -> bool:
        return self._cached_client().get_userprofile_settings() is not None

    def _cached_client(self) -> GarminSessionClient:
        client = self._factory()(is_cn=True)
        client.login(str(self.token_dir))
        return client

    def _factory(self) -> GarminClientFactory:
        if self._client_factory is not None:
            return self._client_factory
        from garminconnect import Garmin

        return cast(GarminClientFactory, Garmin)

    @classmethod
    def _document_rows(
        cls, kind: str, document: object, fallback_date: date
    ) -> list[dict[str, object]]:
        if document is None:
            return []
        if isinstance(document, list):
            return [
                cls._row(kind, item, fallback_date, index)
                for index, item in enumerate(document)
                if isinstance(item, dict)
            ]
        if isinstance(document, dict):
            return [cls._row(kind, document, fallback_date, 0)]
        return []

    @staticmethod
    def _collection(document: object, key: str | None) -> object:
        if key is None or not isinstance(document, dict):
            return document
        value = document.get(key)
        return value if isinstance(value, list) else []

    @staticmethod
    def _row(
        kind: str, item: dict[str, object], fallback_date: date, index: int
    ) -> dict[str, object]:
        identity_keys = (
            ("activityId",)
            if kind == "activity"
            else ("samplePk", "timestampGMT", "version", "calendarDate", "date")
            if kind == "body-weight"
            else ("calendarDate", "date", "summaryId", "id")
        )
        source_id = next(
            (
                item[key]
                for key in identity_keys
                if item.get(key) is not None
            ),
            f"{fallback_date.isoformat()}:{kind}:{index}",
        )
        date_key, date_value = next(
            (
                (key, item[key])
                for key in (
                    "startTimeGMT",
                    "startTimeLocal",
                    "calendarDate",
                    "date",
                    "startDate",
                )
                if item.get(key) is not None
            ),
            ("calendarDate", fallback_date.isoformat()),
        )
        occurred = _parse_garmin_datetime(date_key, date_value)
        row: dict[str, object] = {
            "kind": kind,
            "source_id": str(source_id),
            "occurred_at": occurred.isoformat(),
            "payload": item,
        }
        if kind == "activity":
            activity_date = occurred.date()
            start_time_local = item.get("startTimeLocal")
            if isinstance(start_time_local, str):
                with suppress(ValueError):
                    activity_date = _parse_garmin_datetime(
                        "startTimeLocal", start_time_local
                    ).date()
            row["calendar_date"] = activity_date.isoformat()
        return row


def _parse_garmin_datetime(field: str, value: object) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    text = str(value).strip().replace(" ", "T").replace("Z", "+00:00")
    if "T" not in text:
        return datetime.combine(date.fromisoformat(text[:10]), time.min, tzinfo=CN_TIMEZONE)
    occurred = datetime.fromisoformat(text)
    if occurred.tzinfo is None:
        return occurred.replace(tzinfo=UTC if field.endswith("GMT") else CN_TIMEZONE)
    return occurred


def _summary_hash(summary: dict[str, object]) -> str:
    encoded = json.dumps(
        summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def known_activity_summaries(
    records: list[CanonicalRecord], *, source: str = "garmin-cn"
) -> dict[str, str]:
    """Skip complete, unchanged activities while backfilling legacy summary-only rows."""

    known: dict[str, str] = {}
    for record in records:
        if record.kind != "activity" or record.data.get("detail_status") != "complete":
            continue
        digest = record.data.get("garmin_summary_sha256")
        if not isinstance(digest, str):
            continue
        for evidence in record.evidence:
            if evidence.source == source:
                known[evidence.source_id] = digest
    return known


def activity_detail_coverage(
    records: list[CanonicalRecord], *, source: str = "garmin-cn"
) -> dict[str, int]:
    """Count current Garmin activities by locally available detail level."""

    coverage = {"complete": 0, "partial": 0, "summary_only": 0}
    for record in records:
        if record.kind != "activity" or not any(
            item.source == source for item in record.evidence
        ):
            continue
        status = record.data.get("detail_status")
        coverage[status if status in {"complete", "partial"} else "summary_only"] += 1
    return coverage


def _activity_laps(sections: dict[str, object]) -> list[dict[str, object]]:
    for document in (sections.get("laps"), sections.get("activity")):
        if isinstance(document, dict):
            candidate = document.get("lapDTOs")
            if isinstance(candidate, list):
                return [lap for lap in candidate if isinstance(lap, dict)]
        elif isinstance(document, list):
            return [lap for lap in document if isinstance(lap, dict)]
    return []


def _canonical_payload(kind: str, payload: dict[str, object]) -> dict[str, JsonValue]:
    if kind == "activity":
        sections = _object(payload.get("_activity_sections")) or {}
        full_activity = _object(sections.get("activity")) or {}
        sync = _object(payload.get("_sync")) or {}
        combined = {**payload, **full_activity}
        activity_type = combined.get("activityType")
        sport = activity_type.get("typeKey") if isinstance(activity_type, dict) else None
        duration_seconds = _number(combined.get("movingDuration"))
        if duration_seconds is None:
            duration_seconds = _number(combined.get("duration"))
        power = _first_number(combined, "avgPower", "averagePower")
        cadence = _first_number(
            combined, "averageBikingCadenceInRevPerMinute", "avgCadence"
        )
        raw_laps = _activity_laps(sections)
        laps = [
            {
                "duration_min": (
                    duration / 60
                    if (duration := _first_number(lap, "movingDuration", "duration"))
                    is not None
                    else None
                ),
                "distance_m": _number(lap.get("distance")),
                "avg_hr": _first_number(lap, "averageHR", "averageHeartRate"),
                "avg_power_w": _first_number(lap, "averagePower", "avgPower"),
            }
            for lap in raw_laps[:200]
        ]
        missing = sync.get("missing_sections")
        detail_document = _object(sections.get("details")) or {}
        recorded_samples = detail_document.get("activityDetailMetrics")
        critical_missing = (
            isinstance(missing, list)
            and any(name in missing for name in ("activity", "details", "original_fit_zip"))
        )
        return cast(
            dict[str, JsonValue],
            {
                "name": _scalar(combined.get("activityName")),
                "sport": _scalar(sport),
                "duration_min": duration_seconds / 60 if duration_seconds is not None else None,
                "distance_m": _number(combined.get("distance")),
                "training_load": _number(combined.get("activityTrainingLoad")),
                "high_intensity_min": _non_negative_number(
                    combined.get("vigorousIntensityMinutes")
                ),
                "avg_hr": _number(combined.get("averageHR")),
                "max_hr": _number(combined.get("maxHR")),
                "avg_speed_mps": _number(combined.get("averageSpeed")),
                **({"avg_power_w": power} if power is not None else {}),
                **({"avg_cadence_rpm": cadence} if cadence is not None else {}),
                "elevation_gain_m": _number(combined.get("elevationGain")),
                "calories": _number(combined.get("calories")),
                "steps": _number(combined.get("steps")),
                "device_id": _scalar(combined.get("deviceId")),
                **({"pool_length_m": _number(combined.get("poolLength"))}
                   if combined.get("poolLength") is not None else {}),
                **({"lap_count": len(raw_laps), "laps": laps} if raw_laps else {}),
                **(
                    {
                        "garmin_summary_sha256": sync.get("summary_sha256"),
                        "detail_status": "partial" if critical_missing else "complete",
                        "detail_missing_sections": missing if isinstance(missing, list) else [],
                        "original_fit_zip_sha256": sync.get("original_fit_zip_sha256"),
                        "recorded_sample_count": (
                            sync.get("recorded_sample_count")
                            if isinstance(sync.get("recorded_sample_count"), int)
                            else len(recorded_samples) if isinstance(recorded_samples, list) else 0
                        ),
                    }
                    if sync else {}
                ),
            },
        )
    if kind == "daily-summary":
        movement = _object(payload.get("movement")) or {}
        heart_rate = _object(payload.get("heartRate")) or {}
        body_battery = _object(payload.get("bodyBattery")) or {}
        stress = _object(payload.get("stress")) or {}
        return cast(
            dict[str, JsonValue],
            {
                "calendar_date": _scalar(payload.get("calendarDate")),
                "steps": _coalesce_number(
                    _first_number(payload, "totalSteps"),
                    _first_number(movement, "steps"),
                ),
                "resting_hr": _coalesce_number(
                    _first_number(payload, "restingHeartRate"),
                    _first_number(heart_rate, "restingValue"),
                ),
                "body_battery": _coalesce_number(
                    _first_number(payload, "bodyBatteryMostRecentValue"),
                    _first_number(body_battery, "latestValue", "maxValue"),
                ),
                "average_stress": _coalesce_number(
                    _first_number(payload, "averageStressLevel"),
                    _first_number(stress, "avgLevel"),
                ),
                "sleep_seconds": _number(payload.get("sleepingSeconds")),
            },
        )
    if kind == "sleep":
        summary = (
            _object(payload.get("dailySleepDTO"))
            or _object(payload.get("values"))
            or payload
        )
        return cast(
            dict[str, JsonValue],
            {
                "calendar_date": _scalar(
                    payload.get("calendarDate") or summary.get("calendarDate")
                ),
                "sleep_seconds": _first_number(
                    summary,
                    "sleepTimeSeconds",
                    "sleepingSeconds",
                    "totalSleepSeconds",
                    "totalSleepTimeInSeconds",
                ),
            },
        )
    if kind == "hrv":
        summary = _object(payload.get("hrvSummary")) or payload
        last_night = _first_number(summary, "lastNightAvg")
        if last_night is not None and not 0 < last_night <= 500:
            last_night = None
        weekly = _first_number(summary, "weeklyAvg")
        if weekly is not None and not 0 < weekly <= 500:
            weekly = None
        return cast(
            dict[str, JsonValue],
            {
                "calendar_date": _scalar(summary.get("calendarDate")),
                # A rolling weekly average is not an overnight observation.
                "hrv": last_night,
                "last_night_avg_ms": last_night,
                "weekly_avg_ms": weekly,
            },
        )
    if kind == "heart-rate":
        values = _object(payload.get("values")) or payload
        return cast(
            dict[str, JsonValue],
            {
                "calendar_date": _scalar(payload.get("calendarDate")),
                "resting_hr": _first_number(values, "restingHeartRate", "restingHR"),
                "min_hr": _first_number(values, "minHeartRate", "wellnessMinAvgHR"),
                "max_hr": _first_number(values, "maxHeartRate", "wellnessMaxAvgHR"),
            },
        )
    if kind == "stress":
        values = _object(payload.get("values")) or payload
        return cast(
            dict[str, JsonValue],
            {
                "calendar_date": _scalar(payload.get("calendarDate")),
                "average_stress": _first_number(
                    values, "averageStressLevel", "overallStressLevel"
                ),
                "max_stress": _number(values.get("maxStressLevel")),
            },
        )
    if kind == "steps":
        return cast(
            dict[str, JsonValue],
            {
                "calendar_date": _scalar(payload.get("calendarDate")),
                "steps": _first_number(payload, "steps", "totalSteps"),
            },
        )
    if kind == "body-battery":
        values = _object(payload.get("values")) or payload
        return cast(
            dict[str, JsonValue],
            {
                "calendar_date": _scalar(payload.get("calendarDate") or payload.get("date")),
                "body_battery": _latest_body_battery(values),
            },
        )
    if kind == "body-weight":
        return cast(
            dict[str, JsonValue],
            {
                "calendar_date": _scalar(
                    payload.get("calendarDate") or payload.get("startDate")
                ),
                "weight_kg": _weight_kg(payload),
            },
        )
    return cast(dict[str, JsonValue], payload)


def _object(value: object) -> dict[str, object] | None:
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def _scalar(value: object) -> str | int | float | bool | None:
    return value if value is None or isinstance(value, (str, int, float, bool)) else None


def _number(value: object) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _non_negative_number(value: object) -> float | int | None:
    number = _number(value)
    return number if number is not None and number >= 0 else None


def _first_number(payload: dict[str, object], *keys: str) -> float | int | None:
    return next(
        (number for key in keys if (number := _number(payload.get(key))) is not None),
        None,
    )


def _coalesce_number(*values: float | int | None) -> float | int | None:
    return next((value for value in values if value is not None), None)


def _latest_body_battery(payload: dict[str, object]) -> float | int | None:
    direct = _first_number(
        payload,
        "bodyBatteryMostRecentValue",
        "latestValue",
        "highBodyBattery",
        "maxValue",
    )
    if direct is not None:
        return direct
    values = payload.get("bodyBatteryValuesArray")
    if not isinstance(values, list):
        return None
    for item in reversed(values):
        if isinstance(item, list) and len(item) >= 2:
            value = _number(item[1])
            if value is not None:
                return value
    return None


def _weight_kg(payload: dict[str, object]) -> float | int | None:
    kilograms = _first_number(payload, "weight_kg", "weightKilograms")
    if kilograms is not None:
        return kilograms
    grams = _first_number(payload, "weight", "totalAverage")
    return grams / 1000 if grams is not None else None
