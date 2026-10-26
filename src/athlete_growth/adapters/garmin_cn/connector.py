"""Read-only Garmin Connect China session connector."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, date, datetime, time, timedelta, timezone
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

    def __init__(self, *, home: Path, gateway: GarminGateway | None = None) -> None:
        self.home = home
        self.session_token_dir = home / "tokens" / "garmin-cn-session"
        self.session_token_file = self.session_token_dir / "garmin_tokens.json"
        self._gateway = gateway

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

    def authenticate(
        self, *, email: str, password: str, prompt_mfa: Callable[[], str]
    ) -> dict[str, object]:
        gateway = self._gateway or GarminCnSessionGateway(token_dir=self.session_token_dir)
        return gateway.authenticate(email, password, prompt_mfa)

    def sync(self, window: SyncWindow, cursor: str | None) -> SyncBatch:
        gateway = self._gateway or GarminCnSessionGateway(token_dir=self.session_token_dir)
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
        return RawSourceRecord(
            source="garmin-cn",
            kind=str(row["kind"]),
            source_id=str(row["source_id"]),
            occurred_at=occurred.astimezone(UTC),
            payload=cast(dict[str, JsonValue], payload),
            canonical_payload=_canonical_payload(str(row["kind"]), payload),
        )


class GarminCnSessionGateway:
    """Thin read-only composition around the open-source ``garminconnect`` client."""

    def __init__(
        self,
        *,
        token_dir: Path,
        client_factory: GarminClientFactory | None = None,
    ) -> None:
        self.token_dir = token_dir
        self._client_factory = client_factory

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
            rows.append(self._row("activity", activity, start, index))

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
        return {
            "kind": kind,
            "source_id": str(source_id),
            "occurred_at": occurred.isoformat(),
            "payload": item,
        }


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


def _canonical_payload(kind: str, payload: dict[str, object]) -> dict[str, JsonValue]:
    if kind == "activity":
        activity_type = payload.get("activityType")
        sport = activity_type.get("typeKey") if isinstance(activity_type, dict) else None
        duration_seconds = _number(payload.get("movingDuration"))
        if duration_seconds is None:
            duration_seconds = _number(payload.get("duration"))
        return cast(
            dict[str, JsonValue],
            {
                "name": _scalar(payload.get("activityName")),
                "sport": _scalar(sport),
                "duration_min": duration_seconds / 60 if duration_seconds is not None else None,
                "distance_m": _number(payload.get("distance")),
                "training_load": _number(payload.get("activityTrainingLoad")),
                "high_intensity_min": _non_negative_number(
                    payload.get("vigorousIntensityMinutes")
                ),
                "avg_hr": _number(payload.get("averageHR")),
                "max_hr": _number(payload.get("maxHR")),
                "avg_speed_mps": _number(payload.get("averageSpeed")),
                "elevation_gain_m": _number(payload.get("elevationGain")),
                "calories": _number(payload.get("calories")),
                "steps": _number(payload.get("steps")),
                "device_id": _scalar(payload.get("deviceId")),
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
        return cast(
            dict[str, JsonValue],
            {
                "calendar_date": _scalar(summary.get("calendarDate")),
                "hrv": _first_number(summary, "lastNightAvg", "weeklyAvg", "hrv"),
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
