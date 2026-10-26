"""Private per-user launchd scheduling for the automatic training loop."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

LABEL = "com.athlete-growth.automation"
_LAUNCHCTL = "/bin/launchctl"


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
        self.data_home = data_home.expanduser().absolute()
        self.python_executable = python_executable.expanduser().absolute()
        self.home = (home or Path.home()).expanduser().absolute()
        self.uid = os.getuid() if uid is None else uid
        self.timeout_seconds = timeout_seconds

    @property
    def plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    def install(self) -> dict[str, object]:
        self._require_macos()
        launch_agents = self.plist_path.parent
        logs = self.data_home / "logs"
        launch_agents.mkdir(parents=True, exist_ok=True, mode=0o700)
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(launch_agents, 0o700)
        os.chmod(logs, 0o700)
        for name in ("automation.stdout.log", "automation.stderr.log"):
            descriptor = os.open(
                logs / name,
                os.O_WRONLY
                | os.O_APPEND
                | os.O_CREAT
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
        self._write_plist_atomically(self._payload())
        self._launchctl("bootout", check=False)
        self._launchctl("bootstrap", check=True)
        return {"installed": True, "label": LABEL}

    def uninstall(self) -> dict[str, object]:
        self._require_macos()
        self._launchctl("bootout", check=False)
        self.plist_path.unlink(missing_ok=True)
        return {"installed": False, "label": LABEL}

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
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.plist_path.parent,
            prefix=f".{LABEL}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            os.chmod(temporary_path, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                plistlib.dump(payload, handle, fmt=plistlib.FMT_XML, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.plist_path)
            os.chmod(self.plist_path, 0o600)
        except BaseException:
            with suppress(OSError):
                os.close(descriptor)
            with suppress(FileNotFoundError):
                temporary_path.unlink()
            raise

    def _launchctl(self, verb: str, *, check: bool) -> None:
        try:
            subprocess.run(
                [
                    _LAUNCHCTL,
                    verb,
                    f"gui/{self.uid}",
                    str(self.plist_path),
                ],
                check=check,
                shell=False,
                timeout=self.timeout_seconds,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"launchctl {verb} failed") from exc

    @staticmethod
    def _require_macos() -> None:
        if sys.platform != "darwin":
            raise RuntimeError("the launchd scheduler is available only on macOS")
