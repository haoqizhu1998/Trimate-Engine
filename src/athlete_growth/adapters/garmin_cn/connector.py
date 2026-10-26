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

    def get_user_summary(self, cdate: str) -> dict[str, object]: ...

    def get_steps_data(self, cdate: str) -> list[dict[str, object]]: ...

    def get_sleep_data(self, cdate: str) -> dict[str, object]: ...

    def get_hrv_data(self, cdate: str) -> dict[str, object] | None: ...

    def get_heart_rates(self, cdate: str) -> dict[str, object]: ...

    def get_stress_data(self, cdate: str) -> dict[str, object]: ...

    def get_body_battery(
        self, startdate: str, enddate: str
    ) -> list[dict[str, object]]: ...

    def get_weigh_ins(self, startdate: str, enddate: str) -> dict[str, object]: ...


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
            occurred_at=occurred,
            payload=cast(dict[str, JsonValue], payload),
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

        current = start
        while current <= end:
            current_text = current.isoformat()
            daily_calls: tuple[tuple[str, Callable[[str], object]], ...] = (
                ("daily-summary", client.get_user_summary),
                ("steps", client.get_steps_data),
                ("sleep", client.get_sleep_data),
                ("hrv", client.get_hrv_data),
                ("heart-rate", client.get_heart_rates),
                ("stress", client.get_stress_data),
            )
            for kind, fetch_daily in daily_calls:
                document = fetch_daily(current_text)
                rows.extend(self._document_rows(kind, document, current))
            current = date.fromordinal(current.toordinal() + 1)

        rows.extend(
            self._document_rows(
                "body-battery", client.get_body_battery(start_text, end_text), start
            )
        )
        rows.extend(
            self._document_rows(
                "body-weight", client.get_weigh_ins(start_text, end_text), start
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
    def _row(
        kind: str, item: dict[str, object], fallback_date: date, index: int
    ) -> dict[str, object]:
        source_id = next(
            (
                item[key]
                for key in ("activityId", "calendarDate", "date", "summaryId", "id")
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
