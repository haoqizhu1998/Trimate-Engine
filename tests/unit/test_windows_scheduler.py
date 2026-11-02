from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from athlete_growth.adapters.scheduler.windows import TASK_NAME, WindowsTaskScheduler


class WindowsSchedulerTests(unittest.TestCase):
    @patch("athlete_growth.adapters.scheduler.windows.sys.platform", "win32")
    @patch("athlete_growth.adapters.scheduler.windows.subprocess.run")
    def test_creates_and_removes_only_product_task(self, run) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        scheduler = WindowsTaskScheduler(
            data_home=Path("/synthetic/data"), python_executable=Path("/synthetic/python.exe")
        )
        self.assertTrue(scheduler.install()["installed"])
        created = run.call_args.args[0]
        self.assertEqual(created[:3], ["schtasks.exe", "/Create", "/SC"])
        self.assertEqual(created[created.index("/TN") + 1], TASK_NAME)
        self.assertIn("automation-run-once", created[created.index("/TR") + 1])
        self.assertFalse(scheduler.uninstall()["installed"])
        self.assertEqual(run.call_args.args[0], [
            "schtasks.exe", "/Delete", "/TN", TASK_NAME, "/F"
        ])


if __name__ == "__main__":
    unittest.main()
