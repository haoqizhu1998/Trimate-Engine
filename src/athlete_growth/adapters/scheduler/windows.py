"""Per-user Windows Task Scheduler adapter for the automatic training loop."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TASK_NAME = "AthleteGrowthTrainingSync"


class WindowsTaskScheduler:
    def __init__(self, *, data_home: Path, python_executable: Path) -> None:
        self.data_home = data_home.expanduser().absolute()
        self.python_executable = python_executable.expanduser().absolute()

    def install(self) -> dict[str, object]:
        self._require_windows()
        command = subprocess.list2cmdline([
            str(self.python_executable), "-m", "athlete_growth.interfaces.cli.main",
            "automation-run-once", "--data-home", str(self.data_home),
        ])
        self._run([
            "schtasks.exe", "/Create", "/SC", "MINUTE", "/MO", "5",
            "/TN", TASK_NAME, "/TR", command, "/F", "/RL", "LIMITED",
        ])
        return {"installed": True, "label": TASK_NAME}

    def uninstall(self) -> dict[str, object]:
        self._require_windows()
        self._run(["schtasks.exe", "/Delete", "/TN", TASK_NAME, "/F"])
        return {"installed": False, "label": TASK_NAME}

    @staticmethod
    def _run(command: list[str]) -> None:
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=15, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("Windows Task Scheduler command failed") from exc
        if result.returncode:
            raise RuntimeError("Windows Task Scheduler command failed")

    @staticmethod
    def _require_windows() -> None:
        if sys.platform != "win32":
            raise RuntimeError("Windows Task Scheduler is available only on Windows")
