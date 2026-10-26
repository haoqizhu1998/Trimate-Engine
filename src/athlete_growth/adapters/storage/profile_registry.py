"""Local profile registry stored outside profile data databases."""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from tempfile import NamedTemporaryFile


class ProfileRegistry:
    """Track local profiles without copying health data into configuration."""

    def __init__(self, config_file: Path) -> None:
        self._config_file = config_file

    def register(self, athlete_id: str) -> None:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", athlete_id) is None:
            raise ValueError("athlete ID must be a safe configuration identifier")
        document = self._read()
        values = document.get("profile_ids", [])
        profile_ids = (
            [value for value in values if isinstance(value, str)]
            if isinstance(values, list)
            else []
        )
        if athlete_id not in profile_ids:
            profile_ids.append(athlete_id)
        configured_default = document.get("default_profile_id")
        default_profile_id = (
            configured_default if isinstance(configured_default, str) else athlete_id
        )
        self._write(default_profile_id, profile_ids)

    def unregister(self, athlete_id: str) -> None:
        document = self._read()
        values = document.get("profile_ids", [])
        profile_ids = (
            [value for value in values if isinstance(value, str) and value != athlete_id]
            if isinstance(values, list)
            else []
        )
        configured_default = document.get("default_profile_id")
        default_profile_id = (
            configured_default
            if isinstance(configured_default, str) and configured_default in profile_ids
            else (profile_ids[0] if profile_ids else None)
        )
        self._write(default_profile_id, profile_ids)

    def default_profile_id(self) -> str | None:
        value = self._read().get("default_profile_id")
        return value if isinstance(value, str) else None

    def profile_ids(self) -> tuple[str, ...]:
        values = self._read().get("profile_ids", [])
        if not isinstance(values, list):
            return ()
        return tuple(value for value in values if isinstance(value, str))

    def _read(self) -> dict[str, object]:
        if not self._config_file.exists():
            return {}
        with self._config_file.open("rb") as handle:
            document = tomllib.load(handle)
        schema_version = document.get("schema_version")
        if schema_version != 1:
            raise ValueError(f"unsupported config schema version: {schema_version!r}")
        return document

    def _write(self, default_profile_id: str | None, profile_ids: list[str]) -> None:
        parent = self._config_file.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(parent, 0o700)
        quoted_ids = ", ".join(f'"{value}"' for value in profile_ids)
        default_line = (
            f'default_profile_id = "{default_profile_id}"\n'
            if default_profile_id is not None
            else ""
        )
        content = "schema_version = 1\n" + default_line + f"profile_ids = [{quoted_ids}]\n"
        temporary_name: str | None = None
        try:
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=parent,
                prefix=".config.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_name = handle.name
                os.chmod(temporary_name, 0o600)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self._config_file)
            os.chmod(self._config_file, 0o600)
        finally:
            if temporary_name is not None and os.path.exists(temporary_name):
                os.unlink(temporary_name)
