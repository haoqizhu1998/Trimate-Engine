"""Profile export, backup, restore, correction and deletion use cases."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile

from athlete_growth.adapters.storage.vault import VaultLayout


@dataclass(frozen=True, slots=True)
class ArchiveVerification:
    valid: bool
    athlete_id: str | None
    errors: tuple[str, ...] = ()


class DataRightsService:
    """User-controlled, profile-scoped data portability operations."""

    def __init__(self, home: Path) -> None:
        self.home = home

    def export_profile(self, athlete_id: str, destination: Path) -> Path:
        layout = VaultLayout(self.home, athlete_id)
        if not layout.profile_root.is_dir():
            raise FileNotFoundError(layout.profile_root)
        files = [
            path
            for path in sorted(layout.profile_root.rglob("*"))
            if path.is_file() and layout.backups not in path.parents
        ]
        manifest_files: dict[str, str] = {}
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
            for path in files:
                relative = path.relative_to(layout.profile_root).as_posix()
                payload = path.read_bytes()
                manifest_files[relative] = hashlib.sha256(payload).hexdigest()
                archive.writestr(f"profile/{relative}", payload)
            archive.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "schema_version": 1,
                        "athlete_id": athlete_id,
                        "created_at": datetime.now(UTC).isoformat(),
                        "files": manifest_files,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
            )
        os.chmod(destination, 0o600)
        return destination

    def backup_profile(self, athlete_id: str) -> Path:
        layout = VaultLayout(self.home, athlete_id)
        layout.ensure()
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return self.export_profile(athlete_id, layout.backups / f"profile-{timestamp}.zip")

    def verify_archive(self, archive_path: Path) -> ArchiveVerification:
        errors: list[str] = []
        athlete_id: str | None = None
        try:
            with ZipFile(archive_path) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                athlete_id = manifest.get("athlete_id")
                for relative, expected_hash in manifest.get("files", {}).items():
                    safe = self._safe_relative(relative)
                    payload = archive.read(f"profile/{safe.as_posix()}")
                    actual_hash = hashlib.sha256(payload).hexdigest()
                    if actual_hash != expected_hash:
                        errors.append(f"hash mismatch: {relative}")
        except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"invalid archive: {type(exc).__name__}")
        return ArchiveVerification(not errors, athlete_id, tuple(errors))

    def restore_profile(self, archive_path: Path, target_home: Path) -> VaultLayout:
        verification = self.verify_archive(archive_path)
        if not verification.valid or verification.athlete_id is None:
            raise ValueError("archive verification failed")
        layout = VaultLayout(target_home, verification.athlete_id)
        if layout.profile_root.exists():
            raise FileExistsError(layout.profile_root)
        layout.ensure()
        with ZipFile(archive_path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            for relative in manifest["files"]:
                safe = self._safe_relative(relative)
                destination = layout.profile_root.joinpath(*safe.parts)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                destination.write_bytes(archive.read(f"profile/{safe.as_posix()}"))
                os.chmod(destination, 0o600)
        return layout

    def delete_profile(self, athlete_id: str, *, confirmation: str) -> None:
        if confirmation != athlete_id:
            raise ValueError("profile deletion confirmation does not match athlete ID")
        layout = VaultLayout(self.home, athlete_id)
        if layout.profile_root.exists():
            shutil.rmtree(layout.profile_root)

    @staticmethod
    def _safe_relative(value: str) -> PurePosixPath:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError("unsafe archive path")
        return path
