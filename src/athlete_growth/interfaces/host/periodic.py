"""Current-profile calendar summary and Trimate poster capabilities."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from athlete_growth.adapters.posters import trimate_training_summary as poster
from athlete_growth.application.periodic import PeriodicSummaryService, poster_payload
from athlete_growth.interfaces.host.tools import HostRuntime


def _service(data_home: str | Path) -> PeriodicSummaryService:
    runtime = HostRuntime.load(data_home)
    profile = runtime.profiles.get_by_id(runtime.athlete_id)
    if profile is None:
        raise RuntimeError("default profile record is missing")
    return PeriodicSummaryService(
        runtime.athlete_id, runtime.records,
        timezone_name=profile.timezone_name, now=datetime.now(UTC),
    )


def periodic_summary(
    data_home: str | Path, cycle_type: str, anchor_date: date | None = None
) -> dict[str, object]:
    return _service(data_home).summary(cycle_type, anchor_date)


def periodic_poster(
    data_home: str | Path, cycle_type: str, anchor_date: date | None = None
) -> dict[str, object]:
    summary = periodic_summary(data_home, cycle_type, anchor_date)
    training = summary["training_data"]
    assert isinstance(training, dict)
    if training["total_sessions"] == 0:
        raise ValueError("no activities in period")
    with TemporaryDirectory(prefix="athlete-growth-poster-") as temporary:
        source = Path(temporary) / "poster.svg"
        output = Path(temporary) / "poster.png"
        source.write_text(
            poster.render_svg(poster.normalize_poster_data(poster_payload(summary))),
            encoding="utf-8",
        )
        swift = shutil.which("swift")
        if swift is None:
            raise RuntimeError("poster renderer unavailable")
        cache = Path(data_home).expanduser().resolve() / "cache" / "poster-swift"
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = os.environ.copy()
        env["CLANG_MODULE_CACHE_PATH"] = str(cache / "clang")
        env["SWIFT_MODULE_CACHE_PATH"] = str(cache / "swift")
        command = [
            swift,
            str(Path(poster.__file__).with_name("svg_to_png.swift")),
            str(source), str(output),
        ]
        completed = subprocess.run(
            command, env=env, capture_output=True, text=True,
            check=False, timeout=90,
        )
        if completed.returncode != 0 or not output.is_file():
            raise RuntimeError("poster renderer failed")
        data = base64.b64encode(output.read_bytes()).decode("ascii")
    return {
        "cycle_type": cycle_type,
        "start_date": summary["start_date"],
        "end_date": summary["end_date"],
        "partial": summary["partial"],
        "mime_type": "image/png",
        "_image_base64": data,
    }
