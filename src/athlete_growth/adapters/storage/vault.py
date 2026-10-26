"""Local profile vault layout."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class VaultLayout:
    """Filesystem locations for one local athlete profile."""

    home: Path
    athlete_id: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", self.athlete_id) is None:
            raise ValueError("athlete ID must be a safe filesystem identifier")

    @property
    def profile_root(self) -> Path:
        return self.home / "profiles" / self.athlete_id

    @property
    def raw(self) -> Path:
        return self.profile_root / "raw"

    @property
    def database_dir(self) -> Path:
        return self.profile_root / "db"

    @property
    def database_file(self) -> Path:
        return self.database_dir / "athlete.sqlite3"

    @property
    def derived(self) -> Path:
        return self.profile_root / "derived"

    @property
    def reports(self) -> Path:
        return self.profile_root / "reports"

    @property
    def backups(self) -> Path:
        return self.profile_root / "backups"

    def ensure(self) -> None:
        directories = (
            self.home,
            self.home / "secrets",
            self.home / "tokens",
            self.home / "cache",
            self.home / "exports",
            self.home / "logs",
            self.home / "profiles",
            self.profile_root,
            self.raw,
            self.database_dir,
            self.derived,
            self.reports,
            self.backups,
        )
        for path in directories:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
