"""Private per-user launchd scheduling for the automatic training loop."""

from __future__ import annotations

import os
import plistlib
import stat
import subprocess
import sys
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

LABEL = "com.athlete-growth.automation"
_LAUNCHCTL = "/bin/launchctl"
_NOT_LOADED_RETURN_CODE = 3
_NOT_LOADED_STDERR = "Boot-out failed: 3: No such process"


@dataclass(frozen=True, slots=True)
class _SavedPlist:
    content: bytes
    mode: int


class MacOSLaunchdScheduler:
    """Install or remove the one launch agent owned by Athlete Growth."""

    def __init__(
        self,
        *,
        data_home: Path,
        python_executable: Path,
        home: Path | None = None,
        uid: int | None = None,
        timeout_seconds: float = 10,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.data_home = data_home.expanduser().absolute().resolve(strict=False)
        self.python_executable = python_executable.expanduser().absolute()
        self.home = (home or Path.home()).expanduser().absolute().resolve(strict=False)
        self.uid = os.getuid() if uid is None else uid
        self.timeout_seconds = timeout_seconds

    @property
    def plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    def install(self) -> dict[str, object]:
        self._require_macos()
        logs = self.data_home / "logs"
        log_files = tuple(
            logs / name for name in ("automation.stdout.log", "automation.stderr.log")
        )
        self._preflight_directory_chain(self.home, ("Library", "LaunchAgents"))
        self._preflight_directory_chain(self.data_home, ("logs",))
        self._preflight_regular_file(self.plist_path)
        for log_file in log_files:
            self._preflight_regular_file(log_file)

        self._ensure_directory_chain(self.home, ("Library", "LaunchAgents"))
        self._ensure_directory_chain(self.data_home, ("logs",))
        self._validate_parent(self.plist_path, self.home)
        for log_file in log_files:
            self._validate_parent(log_file, self.data_home)
            self._open_private_log(log_file)

        previous = self._read_plist()
        bootout = self._launchctl("bootout")
        previous_was_loaded = bootout.returncode == 0
        try:
            self._write_plist_atomically(self._payload())
            self._launchctl("bootstrap")
        except BaseException as exc:
            recovery_error = self._restore_after_failed_install(
                previous,
                reload_previous=previous_was_loaded,
            )
            if recovery_error is not None:
                raise RuntimeError(
                    f"{exc}; previous launch agent recovery failed"
                ) from recovery_error
            raise
        return {"installed": True, "label": LABEL}

    def uninstall(self) -> dict[str, object]:
        self._require_macos()
        self._preflight_directory_chain(self.home, ("Library", "LaunchAgents"))
        self._preflight_regular_file(self.plist_path)
        self._launchctl("bootout")
        self._unlink_plist()
        return {"installed": False, "label": LABEL}

    def _open_private_log(self, log_file: Path) -> None:
        self._preflight_regular_file(log_file)
        descriptor = os.open(
            log_file,
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RuntimeError("launchd log path is not a regular file")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    def _restore_after_failed_install(
        self,
        previous: _SavedPlist | None,
        *,
        reload_previous: bool,
    ) -> Exception | None:
        if previous is None:
            try:
                self._unlink_plist()
            except Exception as exc:
                return exc
            return None
        try:
            self._write_bytes_atomically(previous.content, previous.mode)
        except Exception as exc:
            return exc
        if not reload_previous:
            return None
        try:
            self._launchctl("bootstrap")
        except Exception as exc:
            return exc
        return None

    def _read_plist(self) -> _SavedPlist | None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.plist_path, flags)
        except FileNotFoundError:
            return None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError("launch agent plist is not a regular file")
            with os.fdopen(os.dup(descriptor), "rb") as handle:
                return _SavedPlist(handle.read(), stat.S_IMODE(metadata.st_mode))
        finally:
            os.close(descriptor)

    def _unlink_plist(self) -> None:
        try:
            metadata = self.plist_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError("launch agent plist must not be a symlink")
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("launch agent plist is not a regular file")
        self.plist_path.unlink()

    @staticmethod
    def _preflight_regular_file(path: Path) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError(f"path must not be a symlink: {path}")
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"path is not a regular file: {path}")

    @classmethod
    def _preflight_directory_chain(cls, root: Path, parts: tuple[str, ...]) -> None:
        for path in cls._directory_chain(root, parts):
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode):
                raise RuntimeError(f"directory path must not be a symlink: {path}")
            if not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError(f"path is not a directory: {path}")

    @classmethod
    def _ensure_directory_chain(cls, root: Path, parts: tuple[str, ...]) -> Path:
        chain = cls._directory_chain(root, parts)
        final = chain[-1]
        for path in chain:
            created = False
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                path.mkdir(mode=0o700)
                metadata = path.lstat()
                created = True
            if stat.S_ISLNK(metadata.st_mode):
                raise RuntimeError(f"directory path must not be a symlink: {path}")
            if not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError(f"path is not a directory: {path}")
            if created or path == final:
                cls._set_directory_mode(path, 0o700)
        return final

    @staticmethod
    def _directory_chain(root: Path, parts: tuple[str, ...]) -> list[Path]:
        if not root.is_absolute():
            raise ValueError("scheduler roots must be absolute")
        chain: list[Path] = []
        current = Path(root.anchor)
        for component in root.parts[1:]:
            current /= component
            chain.append(current)
        for component in parts:
            current /= component
            chain.append(current)
        return chain

    @staticmethod
    def _set_directory_mode(path: Path, mode: int) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise RuntimeError(f"path is not a directory: {path}")
            os.fchmod(descriptor, mode)
        finally:
            os.close(descriptor)

    @staticmethod
    def _validate_parent(path: Path, root: Path) -> None:
        resolved_root = root.resolve(strict=True)
        resolved_parent = path.parent.resolve(strict=True)
        try:
            resolved_parent.relative_to(resolved_root)
        except ValueError:
            raise RuntimeError("launchd path escapes its declared root") from None

    def _payload(self) -> dict[str, object]:
        return {
            "Label": LABEL,
            "ProgramArguments": [
                str(self.python_executable),
                "-m",
                "athlete_growth.interfaces.cli.main",
                "automation-run-once",
                "--data-home",
                str(self.data_home),
            ],
            "RunAtLoad": True,
            "StartInterval": 300,
            "StandardOutPath": str(self.data_home / "logs" / "automation.stdout.log"),
            "StandardErrorPath": str(self.data_home / "logs" / "automation.stderr.log"),
        }

    def _write_plist_atomically(self, payload: dict[str, object]) -> None:
        content = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)
        self._write_bytes_atomically(content, 0o600)

    def _write_bytes_atomically(self, content: bytes, mode: int) -> None:
        self._validate_parent(self.plist_path, self.home)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.plist_path.parent,
            prefix=f".{LABEL}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.plist_path)
        except BaseException:
            with suppress(OSError):
                os.close(descriptor)
            with suppress(FileNotFoundError):
                temporary_path.unlink()
            raise

    def _launchctl(self, verb: str) -> subprocess.CompletedProcess[str]:
        target = (
            [f"gui/{self.uid}/{LABEL}"]
            if verb == "bootout"
            else [f"gui/{self.uid}", str(self.plist_path)]
        )
        try:
            result = subprocess.run(
                [_LAUNCHCTL, verb, *target],
                check=False,
                shell=False,
                timeout=self.timeout_seconds,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"launchctl {verb} failed") from exc
        if result.returncode == 0:
            return result
        if (
            verb == "bootout"
            and result.returncode == _NOT_LOADED_RETURN_CODE
            and result.stderr.strip() == _NOT_LOADED_STDERR
        ):
            return result
        raise RuntimeError(f"launchctl {verb} failed")

    @staticmethod
    def _require_macos() -> None:
        if sys.platform != "darwin":
            raise RuntimeError("the launchd scheduler is available only on macOS")
