"""Read-only Garmin China Personal API connector."""

from __future__ import annotations

from contextlib import suppress
from datetime import date, datetime, time
from pathlib import Path
from typing import Protocol

from athlete_growth.ports.data_source import (
    HealthStatus,
    RawSourceRecord,
    SourceCapabilities,
    SyncBatch,
    SyncWindow,
)

CN_PERSONAL_API_BASE_URL = "https://personal.connectapi.garmin.cn"


class GarminGateway(Protocol):
    def fetch(self, start: date, end: date) -> list[dict[str, object]]: ...

    def healthcheck(self) -> bool: ...


class GarminCnConnector:
    region = "cn"

    def __init__(self, *, home: Path, gateway: GarminGateway | None = None) -> None:
        self.home = home
        self.api_key_file = home / "secrets" / "garmin-cn-personal-api-key"
        self.token_cache_file = home / "tokens" / "garmin-cn-bearer-token.json"
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

    def sync(self, window: SyncWindow, cursor: str | None) -> SyncBatch:
        gateway = self._gateway or _OfficialGarminGateway(self.api_key_file, self.token_cache_file)
        rows = gateway.fetch(window.start, window.end)
        records = tuple(self._to_raw(row) for row in rows)
        return SyncBatch(records, next_cursor=window.end.isoformat())

    def healthcheck(self) -> HealthStatus:
        try:
            gateway = self._gateway or _OfficialGarminGateway(
                self.api_key_file, self.token_cache_file
            )
            ok = gateway.healthcheck()
            return HealthStatus(ok, "connected" if ok else "service unavailable")
        except Exception as exc:
            return HealthStatus(False, f"{type(exc).__name__}: {exc}")

    def revoke(self) -> None:
        for path in (self.api_key_file, self.token_cache_file):
            with suppress(FileNotFoundError):
                path.unlink()

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
            payload=payload,
        )


class _OfficialGarminGateway:
    """Thin composition around Garmin's official Personal API client."""

    def __init__(self, api_key_file: Path, token_cache_file: Path) -> None:
        self.api_key_file = api_key_file
        self.token_cache_file = token_cache_file

    def _client(self):
        from garmin_personal_api import (
            GarminPersonalApiClient,
            PersonalApiAuthenticator,
            resolve_api_key,
        )

        key = resolve_api_key(key_file=self.api_key_file)
        authenticator = PersonalApiAuthenticator(bearer_token_cache_file=self.token_cache_file)
        authenticator.base_url = CN_PERSONAL_API_BASE_URL
        try:
            token = authenticator.obtain_token(key)
        finally:
            authenticator.close()
        client = GarminPersonalApiClient(token.access_token)
        client.base_url = CN_PERSONAL_API_BASE_URL
        return client

    def fetch(self, start: date, end: date) -> list[dict[str, object]]:
        client = self._client()
        try:
            datasets = {
                "daily-summary": client.get_daily_summaries(start, end),
                "sleep": client.get_daily_sleep_summaries(start, end),
                "stress": client.get_daily_stress(start, end),
                "steps": client.get_daily_steps(start, end),
                "body-battery": client.get_daily_body_battery(start, end),
                "body-weight": client.get_weight_measurements(start, end),
                "hrv": client.get_daily_hrv_summaries(start, end),
                "activity": client.search_activities(
                    start=0, limit=1000, start_date=start, end_date=end
                ),
            }
            current = start
            heart_rate_rows: list[object] = []
            while current <= end:
                heart_rate_rows.append(client.get_daily_heart_rate(current))
                current = date.fromordinal(current.toordinal() + 1)
            datasets["heart-rate"] = heart_rate_rows
        finally:
            client.close()
        rows: list[dict[str, object]] = []
        for kind, document in datasets.items():
            rows.extend(self._rows(kind, document, start))
        return rows

    def healthcheck(self) -> bool:
        client = self._client()
        try:
            return client.get_user_profile_settings() is not None
        finally:
            client.close()

    @staticmethod
    def _rows(kind: str, document: object, fallback_date: date) -> list[dict[str, object]]:
        if document is None:
            return []
        if isinstance(document, dict):
            candidates = next(
                (value for value in document.values() if isinstance(value, list)),
                [document],
            )
        elif isinstance(document, list):
            candidates = document
        else:
            return []
        rows: list[dict[str, object]] = []
        for index, item in enumerate(candidates):
            if not isinstance(item, dict):
                continue
            source_id = next(
                (
                    item[key]
                    for key in ("activityId", "calendarDate", "summaryId", "id")
                    if item.get(key) is not None
                ),
                f"{fallback_date.isoformat()}:{kind}:{index}",
            )
            date_value = next(
                (
                    item[key]
                    for key in ("startTimeGMT", "startTimeLocal", "calendarDate", "date")
                    if item.get(key) is not None
                ),
                fallback_date.isoformat(),
            )
            if isinstance(date_value, (int, float)):
                local_tz = datetime.now().astimezone().tzinfo
                occurred = datetime.fromtimestamp(date_value / 1000, tz=local_tz)
            else:
                text = str(date_value)
                if "T" not in text:
                    occurred = datetime.combine(
                        date.fromisoformat(text[:10]), time.min
                    ).astimezone()
                else:
                    occurred = datetime.fromisoformat(text.replace("Z", "+00:00"))
                    if occurred.tzinfo is None:
                        occurred = occurred.astimezone()
            rows.append(
                {
                    "kind": kind,
                    "source_id": str(source_id),
                    "occurred_at": occurred.isoformat(),
                    "payload": item,
                }
            )
        return rows
