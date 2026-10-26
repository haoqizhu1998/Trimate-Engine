from __future__ import annotations

import os
import plistlib
import stat
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import call, patch

from athlete_growth.adapters.scheduler.macos import (
    LABEL,
    MacOSLaunchdScheduler,
)


class MacOSLaunchdSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.home = self.root / "home"
        self.data_home = self.root / "data"
        self.python = self.root / "python"
        self.scheduler = MacOSLaunchdScheduler(
            data_home=self.data_home,
            python_executable=self.python,
            home=self.home,
            uid=501,
            timeout_seconds=7,
        )

    @staticmethod
    def _result(
        returncode: int = 0,
        *,
        stderr: str = "",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], returncode, stdout="", stderr=stderr)

    @patch("athlete_growth.adapters.scheduler.macos.sys.platform", "darwin")
    @patch("athlete_growth.adapters.scheduler.macos.subprocess.run")
    def test_install_writes_exact_private_plist_and_bootstraps_it(self, run) -> None:
        run.side_effect = [
            self._result(3, stderr="Boot-out failed: 3: No such process\n"),
            self._result(),
        ]
        result = self.scheduler.install()

        plist_path = self.scheduler.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        self.assertEqual(self.scheduler.plist_path, plist_path)
        self.assertEqual(self.scheduler.python_executable, self.python.absolute())
        with plist_path.open("rb") as handle:
            payload = plistlib.load(handle)
        self.assertEqual(
            payload,
            {
                "Label": "com.athlete-growth.automation",
                "ProgramArguments": [
                    str(self.python.absolute()),
                    "-m",
                    "athlete_growth.interfaces.cli.main",
                    "automation-run-once",
                    "--data-home",
                    str(self.scheduler.data_home),
                ],
                "RunAtLoad": True,
                "StartInterval": 300,
                "StandardOutPath": str(self.scheduler.data_home / "logs" / "automation.stdout.log"),
                "StandardErrorPath": str(
                    self.scheduler.data_home / "logs" / "automation.stderr.log"
                ),
            },
        )
        self.assertEqual(stat.S_IMODE(plist_path.stat().st_mode), 0o600)
        for log_name in ("automation.stdout.log", "automation.stderr.log"):
            log_file = self.scheduler.data_home / "logs" / log_name
            self.assertTrue(log_file.is_file())
            self.assertEqual(stat.S_IMODE(log_file.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE((self.scheduler.home / "Library" / "LaunchAgents").stat().st_mode),
            0o700,
        )
        self.assertEqual(
            stat.S_IMODE((self.scheduler.data_home / "logs").stat().st_mode),
            0o700,
        )
        self.assertEqual(result, {"installed": True, "label": LABEL})
        self.assertEqual(
            run.call_args_list,
            [
                call(
                    ["/bin/launchctl", "bootout", "gui/501", str(plist_path)],
                    check=False,
                    shell=False,
                    timeout=7,
                    capture_output=True,
                    text=True,
                ),
                call(
                    ["/bin/launchctl", "bootstrap", "gui/501", str(plist_path)],
                    check=False,
                    shell=False,
                    timeout=7,
                    capture_output=True,
                    text=True,
                ),
            ],
        )

    @patch("athlete_growth.adapters.scheduler.macos.sys.platform", "darwin")
    @patch("athlete_growth.adapters.scheduler.macos.subprocess.run")
    def test_install_atomically_replaces_an_existing_plist(self, run) -> None:
        run.side_effect = [self._result(), self._result()]
        self.scheduler.plist_path.parent.mkdir(parents=True)
        self.scheduler.plist_path.write_bytes(plistlib.dumps({"Label": "stale"}))

        self.scheduler.install()

        with self.scheduler.plist_path.open("rb") as handle:
            self.assertEqual(plistlib.load(handle)["Label"], LABEL)
        self.assertEqual(
            list(self.scheduler.plist_path.parent.glob(f".{LABEL}.*.tmp")),
            [],
        )
        run.assert_called()

    @patch("athlete_growth.adapters.scheduler.macos.sys.platform", "darwin")
    @patch("athlete_growth.adapters.scheduler.macos.subprocess.run")
    def test_uninstall_boots_out_only_exact_target_then_removes_it(self, run) -> None:
        run.return_value = self._result()
        self.scheduler.plist_path.parent.mkdir(parents=True)
        self.scheduler.plist_path.write_text("existing", encoding="utf-8")
        unrelated = self.scheduler.plist_path.parent / "com.example.unrelated.plist"
        unrelated.write_text("keep", encoding="utf-8")

        result = self.scheduler.uninstall()

        self.assertFalse(self.scheduler.plist_path.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(result, {"installed": False, "label": LABEL})
        run.assert_called_once_with(
            ["/bin/launchctl", "bootout", "gui/501", str(self.scheduler.plist_path)],
            check=False,
            shell=False,
            timeout=7,
            capture_output=True,
            text=True,
        )

    @patch("athlete_growth.adapters.scheduler.macos.sys.platform", "darwin")
    @patch("athlete_growth.adapters.scheduler.macos.subprocess.run")
    def test_uninstall_is_idempotent_when_plist_is_absent(self, run) -> None:
        run.return_value = self._result(
            3,
            stderr="Boot-out failed: 3: No such process\n",
        )
        self.assertEqual(
            self.scheduler.uninstall(),
            {"installed": False, "label": LABEL},
        )
        run.assert_called_once_with(
            ["/bin/launchctl", "bootout", "gui/501", str(self.scheduler.plist_path)],
            check=False,
            shell=False,
            timeout=7,
            capture_output=True,
            text=True,
        )

    @patch("athlete_growth.adapters.scheduler.macos.sys.platform", "darwin")
    @patch("athlete_growth.adapters.scheduler.macos.subprocess.run")
    def test_uninstall_permission_error_keeps_plist(self, run) -> None:
        self.scheduler.plist_path.parent.mkdir(parents=True)
        original = b"private launch agent"
        self.scheduler.plist_path.write_bytes(original)
        os.chmod(self.scheduler.plist_path, 0o640)
        run.return_value = self._result(
            5,
            stderr="Boot-out failed: 5: Input/output error\n",
        )

        with self.assertRaisesRegex(RuntimeError, "launchctl bootout failed"):
            self.scheduler.uninstall()

        self.assertEqual(self.scheduler.plist_path.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(self.scheduler.plist_path.stat().st_mode), 0o640)

    @patch("athlete_growth.adapters.scheduler.macos.sys.platform", "darwin")
    @patch("athlete_growth.adapters.scheduler.macos.subprocess.run")
    def test_fresh_bootstrap_failure_removes_new_plist(self, run) -> None:
        run.side_effect = [
            self._result(3, stderr="Boot-out failed: 3: No such process\n"),
            self._result(5, stderr="Bootstrap failed: 5: Input/output error\n"),
        ]

        with self.assertRaisesRegex(RuntimeError, "launchctl bootstrap failed"):
            self.scheduler.install()

        self.assertFalse(self.scheduler.plist_path.exists())

    @patch("athlete_growth.adapters.scheduler.macos.sys.platform", "darwin")
    @patch("athlete_growth.adapters.scheduler.macos.subprocess.run")
    def test_update_bootstrap_failure_restores_and_reloads_previous_plist(self, run) -> None:
        self.scheduler.plist_path.parent.mkdir(parents=True)
        previous = plistlib.dumps({"Label": "previous", "Keep": "exact bytes"})
        self.scheduler.plist_path.write_bytes(previous)
        os.chmod(self.scheduler.plist_path, 0o640)
        run.side_effect = [
            self._result(),
            self._result(5, stderr="Bootstrap failed: 5: Input/output error\n"),
            self._result(),
        ]

        with self.assertRaisesRegex(RuntimeError, "launchctl bootstrap failed"):
            self.scheduler.install()

        self.assertEqual(self.scheduler.plist_path.read_bytes(), previous)
        self.assertEqual(stat.S_IMODE(self.scheduler.plist_path.stat().st_mode), 0o640)
        self.assertEqual(
            [item.args[0][1] for item in run.call_args_list],
            ["bootout", "bootstrap", "bootstrap"],
        )

    @patch("athlete_growth.adapters.scheduler.macos.sys.platform", "darwin")
    @patch("athlete_growth.adapters.scheduler.macos.subprocess.run")
    def test_install_rejects_symlinked_logs_without_external_mutation(self, run) -> None:
        external = self.root / "external-logs"
        external.mkdir(mode=0o750)
        sentinel = external / "sentinel"
        sentinel.write_text("unchanged", encoding="utf-8")
        self.data_home.mkdir()
        (self.data_home / "logs").symlink_to(external, target_is_directory=True)
        before_mode = stat.S_IMODE(external.stat().st_mode)

        with self.assertRaisesRegex(RuntimeError, "symlink"):
            self.scheduler.install()

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
        self.assertEqual(stat.S_IMODE(external.stat().st_mode), before_mode)
        self.assertEqual(list(external.iterdir()), [sentinel])
        self.assertFalse(self.home.exists())
        run.assert_not_called()

    @patch("athlete_growth.adapters.scheduler.macos.sys.platform", "darwin")
    @patch("athlete_growth.adapters.scheduler.macos.subprocess.run")
    def test_install_rejects_symlinked_launch_agents_without_external_mutation(
        self,
        run,
    ) -> None:
        external = self.root / "external-launch-agents"
        external.mkdir(mode=0o750)
        sentinel = external / "sentinel"
        sentinel.write_text("unchanged", encoding="utf-8")
        (self.home / "Library").mkdir(parents=True)
        (self.home / "Library" / "LaunchAgents").symlink_to(
            external,
            target_is_directory=True,
        )
        before_mode = stat.S_IMODE(external.stat().st_mode)

        with self.assertRaisesRegex(RuntimeError, "symlink"):
            self.scheduler.install()

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
        self.assertEqual(stat.S_IMODE(external.stat().st_mode), before_mode)
        self.assertEqual(list(external.iterdir()), [sentinel])
        self.assertFalse(self.data_home.exists())
        run.assert_not_called()

    @patch("athlete_growth.adapters.scheduler.macos.sys.platform", "linux")
    @patch("athlete_growth.adapters.scheduler.macos.subprocess.run")
    def test_non_darwin_refuses_before_any_mutation(self, run) -> None:
        with self.assertRaisesRegex(RuntimeError, "macOS"):
            self.scheduler.install()
        with self.assertRaisesRegex(RuntimeError, "macOS"):
            self.scheduler.uninstall()

        self.assertFalse(self.home.exists())
        self.assertFalse(self.data_home.exists())
        run.assert_not_called()

    def test_constructor_rejects_invalid_timeout(self) -> None:
        with self.assertRaisesRegex(ValueError, "timeout"):
            MacOSLaunchdScheduler(
                data_home=self.data_home,
                python_executable=self.python,
                home=self.home,
                uid=os.getuid(),
                timeout_seconds=0,
            )


if __name__ == "__main__":
    unittest.main()
