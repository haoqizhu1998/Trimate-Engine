from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from athlete_growth.adapters.running_form.contracts import (
    AnalysisConfidence,
    JobStatus,
    RunningFormAssessment,
    Viewpoint,
)
from athlete_growth.interfaces.cli import main as cli_main
from athlete_growth.interfaces.host import running_form as running_form_host
from athlete_growth.interfaces.mcp import server


class TrimateExecutionMCPTests(unittest.TestCase):
    def _init(self, home: Path) -> None:
        with redirect_stdout(StringIO()):
            self.assertEqual(
                cli_main.main([
                    "init", "--name", "Synthetic Athlete", "--timezone", "Asia/Shanghai",
                    "--data-home", str(home),
                ]),
                0,
            )

    def _call(self, home: Path, name: str, arguments: dict[str, object]):
        response = server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": name, "arguments": arguments}},
            home,
        )
        assert response is not None
        return response["result"]

    def _record_activity(self, home: Path) -> dict[str, object]:
        fields = {"sport": "running", "duration_min": 50, "distance_m": 10000, "rpe": 5}
        values = {
            "kind": "activity", "occurred_at": datetime.now(UTC).isoformat(),
            "fields": fields,
        }
        preview = self._call(home, "preview_training_memory", values)["structuredContent"]
        self.assertFalse(preview["saved"])
        changed = self._call(
            home, "confirm_training_memory",
            {**values, "fields": {**fields, "rpe": 8}, "preview_hash": preview["preview_hash"]},
        )
        self.assertTrue(changed["isError"])
        confirmed = self._call(
            home, "confirm_training_memory", {**values, "preview_hash": preview["preview_hash"]}
        )["structuredContent"]
        self.assertTrue(confirmed["saved"])
        duplicate = self._call(
            home, "confirm_training_memory", {**values, "preview_hash": preview["preview_hash"]}
        )["structuredContent"]
        self.assertTrue(duplicate["already_recorded"])
        return confirmed

    def test_confirmed_memory_is_queryable_and_used_in_period_summary(self) -> None:
        with TemporaryDirectory() as temporary:
            home = Path(temporary) / "vault"
            self._init(home)
            saved = self._record_activity(home)
            memory = self._call(home, "list_training_memory", {})["structuredContent"]
            self.assertEqual(memory["count"], 1)
            self.assertEqual(memory["items"][0]["record_id"], saved["record_id"])
            summary = self._call(
                home, "get_periodic_training_summary", {"cycle_type": "week"}
            )["structuredContent"]
            self.assertEqual(summary["training_data"]["total_sessions"], 1)
            self.assertEqual(summary["training_data"]["discipline_breakdown"][0]["sport"],
                             "running")
            self.assertNotIn("completion_rate", str(summary))

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("swift"), "macOS renderer")
    def test_poster_is_returned_as_image_without_vault_path(self) -> None:
        with TemporaryDirectory() as temporary:
            home = Path(temporary) / "vault"
            self._init(home)
            self._record_activity(home)
            response = self._call(
                home, "render_periodic_training_poster", {"cycle_type": "week"}
            )
            self.assertFalse(response["isError"])
            self.assertEqual(response["content"][1]["type"], "image")
            self.assertEqual(response["content"][1]["mimeType"], "image/png")
            self.assertNotIn(str(home), str(response["structuredContent"]))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "video tools")
    def test_running_form_job_is_private_async_and_discards_video(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "vault"
            self._init(home)
            video = root / "synthetic.mp4"
            subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                 "color=c=black:s=160x120:r=10", "-t", "1", "-pix_fmt", "yuv420p",
                 str(video)], check=True, capture_output=True,
            )
            staged = running_form_host.stage_video(home, video, note="testing")
            job_id = str(staged["job_id"])
            initial = self._call(home, "get_running_form_job", {"job_id": job_id})[
                "structuredContent"
            ]
            self.assertEqual(initial["status"], "received")
            runtime = self._call(home, "get_running_form_runtime", {})["structuredContent"]
            self.assertIn("available", runtime)
            with patch.object(
                running_form_host, "runtime_status",
                return_value={"available": False, "model_installed": False},
            ):
                started = self._call(
                    home, "start_running_form_analysis", {"job_id": job_id}
                )["structuredContent"]
            self.assertEqual(started["status"], "setup_required")

            class FakePipeline:
                def analyze(self, job, *, user_directory):
                    assessment = RunningFormAssessment(
                        id=job.id.replace("rfj_", "rfa_", 1),
                        user_id=job.user_id, status=JobStatus.REPORTING,
                        rule_version="test", service_version="test",
                        viewpoints=(Viewpoint.SIDE,), coverage=0.2,
                        confidence=AnalysisConfidence.LOW,
                        dimension_scores={
                            "posture": 70.0, "braking": None, "symmetry": None,
                            "coordination": None, "economy": None,
                        },
                        overall_score=None, safety_mode=False,
                    )
                    return SimpleNamespace(
                        assessment=assessment, limitations=("侧面视频不能评估左右对称。",),
                        metrics={}, keyframes=(), contact_sheet=None,
                    )

            running_form_host.process_job(home, job_id, pipeline=FakePipeline())
            complete = self._call(
                home, "get_running_form_job", {"job_id": job_id}
            )["structuredContent"]
            self.assertEqual(complete["status"], "completed")
            self.assertIsNone(complete["assessment"]["dimension_scores"]["symmetry"])
            self.assertIn("未评估", complete["assessment"]["report_markdown"])
            self.assertNotIn(str(home), str(complete))
            sheet = self._call(
                home, "get_running_form_contact_sheet", {"job_id": job_id}
            )["structuredContent"]
            self.assertFalse(sheet["available"])
            self.assertFalse(any((home / "profiles").glob("*/running-form/tmp/*/*.mp4")))


if __name__ == "__main__":
    unittest.main()
