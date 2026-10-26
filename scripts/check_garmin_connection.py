#!/usr/bin/env python3
"""Verify Garmin Personal API access without printing personal values."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from garmin_personal_api import (
    GarminPersonalApiClient,
    HttpError,
    PersonalApiAuthenticator,
    resolve_api_key,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
KEY_FILE = PROJECT_ROOT / ".secrets" / "garmin-global-personal-api-key"
TOKEN_CACHE = PROJECT_ROOT / ".secrets" / "garmin-global-bearer-token.json"


def main() -> int:
    api_key = resolve_api_key(key_file=KEY_FILE)
    with PersonalApiAuthenticator(bearer_token_cache_file=TOKEN_CACHE) as authenticator:
        token = authenticator.obtain_token(api_key)
    with GarminPersonalApiClient(token.access_token) as client:
        profile = client.get_user_profile_settings()

    fields = sorted(profile) if isinstance(profile, dict) else []
    print(json.dumps({"connected": True, "profile_fields": fields}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HttpError as exc:
        message = None
        try:
            error_body = json.loads(exc.response_text) if exc.response_text else {}
            if isinstance(error_body, dict):
                message = error_body.get("message") or error_body.get("error")
        except json.JSONDecodeError:
            pass
        print(
            json.dumps(
                {
                    "connected": False,
                    "status": exc.status_code,
                    "method": exc.method,
                    "host": urlparse(exc.url).hostname,
                    "message": message,
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from None
