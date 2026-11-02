"""Private, asynchronous execution adapter for the Trimate video engine."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from athlete_growth.adapters.running_form.media import (
    SUPPORTED_VIDEO_SUFFIXES,
    MediaLimits,
    VideoMediaService,
)
from athlete_growth.interfaces.host.tools import HostRuntime

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
    "pose_landmarker_full/float16/latest/pose_landmarker_full.task"
)
_MAX_NOTE = 1000


def _root(runtime: HostRuntime) -> Path:
    return runtime.layout.derived / "running-form"


def _backend_python(runtime: HostRuntime) -> Path:
    private = runtime.home / "cache" / "running-form-venv" / "bin" / "python"
    return private if private.is_file() else Path(sys.executable)


def setup_backend(data_home: str | Path) -> dict[str, object]:
    """Install the optional video engine in a private Python 3.12 environment."""

    runtime = HostRuntime.load(data_home)
    source_root = next(
        (parent for parent in Path(__file__).resolve().parents
         if (parent / "pyproject.toml").is_file()),
        None,
    )
    if source_root is None:
        raise RuntimeError("running-form setup requires the source checkout")
    python312 = shutil.which("python3.12")
    if python312 is None:
        raise RuntimeError("Python 3.12 is required for running-form setup")
    environment = runtime.home / "cache" / "running-form-venv"
    environment.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = os.environ.copy()
    env["UV_CACHE_DIR"] = str(runtime.home / "cache" / "uv-running-form")
    uv = shutil.which("uv")
    environment_python = environment / "bin" / "python"
    if uv is not None:
        commands = [
            [uv, "venv", "--python", python312, str(environment)],
            [uv, "pip", "install", "--python", str(environment_python),
             "-e", ".[running-form]"],
        ]
    else:
        commands = [
            [python312, "-m", "venv", str(environment)],
            [str(environment_python), "-m", "pip", "install",
             "-e", ".[running-form]"],
        ]
    if environment_python.is_file():
        commands.pop(0)
    for command in commands:
        completed = subprocess.run(
            command, cwd=source_root, capture_output=True, text=True,
            timeout=600, check=False, env=env,
        )
        if completed.returncode != 0:
            if "dns error" in completed.stderr or "Failed to fetch" in completed.stderr:
                raise RuntimeError("running-form backend download unavailable; retry online")
            raise RuntimeError("running-form backend installation failed")
    return runtime_status(data_home)


def _job_path(runtime: HostRuntime, job_id: str) -> Path:
    if not job_id.startswith("rfj_") or len(job_id) != 36 or not all(
        char in "0123456789abcdef" for char in job_id[4:]
    ):
        raise ValueError("invalid running-form job ID")
    return _root(runtime) / "jobs" / f"{job_id}.json"


def _save(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load(runtime: HostRuntime, job_id: str) -> dict[str, object]:
    path = _job_path(runtime, job_id)
    if not path.is_file() or path.is_symlink():
        raise KeyError("running-form job not found")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("athlete_id") != runtime.athlete_id or payload.get("job_id") != job_id:
        raise KeyError("running-form job not found")
    return payload


def runtime_status(data_home: str | Path) -> dict[str, object]:
    runtime = HostRuntime.load(data_home)
    python = _backend_python(runtime)
    if python.resolve() == Path(sys.executable).resolve():
        version = sys.version_info[:2]
        missing = [
            module for module in ("cv2", "mediapipe", "PIL")
            if importlib.util.find_spec(module) is None
        ]
    else:
        probe = subprocess.run(
            [str(python), "-c", "import importlib.util,json,sys;print(json.dumps("
             "{'version':list(sys.version_info[:2]),'missing':[name for name in "
             "('cv2','mediapipe','PIL') if importlib.util.find_spec(name) is None]}))"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if probe.returncode != 0:
            version, missing = (0, 0), ["cv2", "mediapipe", "PIL"]
        else:
            detail = json.loads(probe.stdout)
            version = tuple(detail["version"])
            missing = detail["missing"]
    model = _root(runtime) / "models" / "pose_landmarker_full.task"
    return {
        "available": (3, 11) <= version < (3, 13) and not missing
        and model.is_file() and model.stat().st_size > 1024,
        "backend_python_version": ".".join(map(str, version)),
        "supported_python": (3, 11) <= version < (3, 13),
        "missing_python_modules": missing,
        "model_installed": model.is_file() and model.stat().st_size > 1024,
        "setup_command": "athlete-growth running-form-setup",
        "model_source": MODEL_URL,
    }


def install_model(data_home: str | Path, source: Path | None = None) -> dict[str, object]:
    """Install an official model or a caller-provided compatible bundle into the vault."""

    runtime = HostRuntime.load(data_home)
    destination = _root(runtime) / "models" / "pose_landmarker_full.task"
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination.parent, 0o700)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        if source is not None:
            if not source.is_file() or source.suffix.lower() != ".task":
                raise ValueError("model must be a .task file")
            with source.open("rb") as input_file, temporary.open("xb") as output:
                shutil.copyfileobj(input_file, output, 1024 * 1024)
        else:
            from urllib.request import urlopen

            with urlopen(MODEL_URL, timeout=30) as input_file, temporary.open("xb") as output:
                shutil.copyfileobj(input_file, output, 1024 * 1024)
        size = temporary.stat().st_size
        if not 1024 < size < 100 * 1024 * 1024:
            raise ValueError("model download is invalid")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {"model_installed": True, "size_bytes": size}


def _probe(path: Path) -> dict[str, object]:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise RuntimeError("ffprobe is required to stage video")
    result = subprocess.run(
        [
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate:format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode != 0:
        raise ValueError("corrupt video")
    payload = json.loads(result.stdout)
    streams = payload.get("streams", [])
    duration = float(payload.get("format", {}).get("duration") or 0)
    if not streams or duration <= 0:
        raise ValueError("corrupt video")
    if duration > MediaLimits().max_duration_seconds:
        raise ValueError("video too long")
    return {
        "duration_seconds": round(duration, 2),
        "width": int(streams[0]["width"]),
        "height": int(streams[0]["height"]),
    }


def stage_video(
    data_home: str | Path, source: Path, *, note: str = ""
) -> dict[str, object]:
    """Stage a local file via CLI; never accept a file path through MCP."""

    runtime = HostRuntime.load(data_home)
    path = source.expanduser().resolve(strict=True)
    if not path.is_file() or path.suffix.lower() not in SUPPORTED_VIDEO_SUFFIXES:
        raise ValueError("unsupported video format")
    size = path.stat().st_size
    if size <= 0 or size > MediaLimits().max_bytes:
        raise ValueError("video size is outside allowed limits")
    if len(note) > _MAX_NOTE:
        raise ValueError("video note is too long")
    metadata = _probe(path)
    job_id = "rfj_" + uuid4().hex
    service = VideoMediaService(
        inbound_media_root=runtime.layout.profile_root,
        users_root=runtime.home / "profiles",
    )
    relative = service.stage_video(
        source=path, user_directory=runtime.layout.profile_root, job_id=job_id, index=1
    )
    payload: dict[str, object] = {
        "job_id": job_id,
        "athlete_id": runtime.athlete_id,
        "status": "received",
        "video_relative": relative,
        "note": note,
        "metadata": metadata,
        "created_at": datetime.now(UTC).isoformat(),
    }
    try:
        _save(_job_path(runtime, job_id), payload)
    except Exception:
        (runtime.layout.profile_root / relative).unlink(missing_ok=True)
        raise
    return {"job_id": job_id, "status": "received", "video": metadata}


def start_analysis(data_home: str | Path, job_id: str) -> dict[str, object]:
    runtime = HostRuntime.load(data_home)
    payload = _load(runtime, job_id)
    if payload["status"] != "received":
        return {"job_id": job_id, "status": payload["status"]}
    status = runtime_status(data_home)
    if not status["available"]:
        return {"job_id": job_id, "status": "setup_required", "runtime": status}
    lock = _job_path(runtime, job_id).with_suffix(".lock")
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return {"job_id": job_id, "status": _load(runtime, job_id)["status"]}
    os.close(fd)
    try:
        payload["status"] = "preprocessing"
        _save(_job_path(runtime, job_id), payload)
        subprocess.Popen(
            [str(_backend_python(runtime)), "-m",
             "athlete_growth.interfaces.host.running_form_worker",
             "--data-home", str(runtime.home), "--job-id", job_id],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        payload["status"] = "received"
        _save(_job_path(runtime, job_id), payload)
        lock.unlink(missing_ok=True)
        raise
    return {"job_id": job_id, "status": "preprocessing"}


def get_job(data_home: str | Path, job_id: str) -> dict[str, object]:
    runtime = HostRuntime.load(data_home)
    payload = _load(runtime, job_id)
    result: dict[str, object] = {
        "job_id": job_id,
        "status": payload["status"],
        "created_at": payload["created_at"],
    }
    if payload["status"] == "completed":
        result["assessment"] = payload["assessment"]
    elif payload["status"] == "failed":
        result["error_code"] = payload.get("error_code", "analysis_failed")
    return result


def get_contact_sheet(data_home: str | Path, job_id: str) -> dict[str, object]:
    runtime = HostRuntime.load(data_home)
    payload = _load(runtime, job_id)
    if payload["status"] != "completed":
        raise ValueError("running-form analysis is not complete")
    assessment_id = job_id.replace("rfj_", "rfa_", 1)
    path = runtime.layout.profile_root / "running-form" / "reports" / f"{assessment_id}.png"
    if not path.is_file() or path.is_symlink():
        return {"job_id": job_id, "available": False}
    return {
        "job_id": job_id, "available": True, "mime_type": "image/png",
        "_image_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
    }


def process_job(data_home: str | Path, job_id: str, *, pipeline: Any = None) -> None:
    """Worker entry. A supplied pipeline is used only by tests."""

    runtime = HostRuntime.load(data_home)
    payload = _load(runtime, job_id)
    relative = str(payload["video_relative"])
    video_path = (runtime.layout.profile_root / relative).resolve()
    if not video_path.is_relative_to(runtime.layout.profile_root.resolve()):
        raise ValueError("invalid staged video")
    estimator = None
    try:
        if pipeline is None:
            from athlete_growth.adapters.running_form.mediapipe_pose import (
                MediaPipePoseEstimator,
            )
            from athlete_growth.adapters.running_form.pipeline import LocalRunningFormPipeline

            model = _root(runtime) / "models" / "pose_landmarker_full.task"
            estimator = MediaPipePoseEstimator.from_model_path(model)
            pipeline = LocalRunningFormPipeline(
                estimator=estimator,
                media_service=VideoMediaService(
                    inbound_media_root=runtime.layout.profile_root,
                    users_root=runtime.home / "profiles",
                ),
            )
        from athlete_growth.adapters.running_form.contracts import JobStatus, RunningFormJob
        from athlete_growth.adapters.running_form.reporting import render_running_form_report

        job = RunningFormJob(
            id=job_id, user_id=runtime.athlete_id, idempotency_key=job_id,
            status=JobStatus.POSE, channel="local", account_id="", peer_id="",
            session_key="", staged_media=(relative,),
            input_context={"note": payload.get("note", "")}, retry_count=0,
            retry_at=None, lease_token=None, lease_expires_at=None,
            delivery_status="pending", cleanup_status="pending", cleanup_retry_count=0,
            cleanup_completed_at=None, last_error_code=None,
        )
        artifacts = pipeline.analyze(job, user_directory=runtime.layout.profile_root)
        assessment = artifacts.assessment
        confidence = {"low": 0.45, "medium": 0.7, "high": 0.9}[
            assessment.confidence.value
        ]
        limitations = list(artifacts.limitations)
        metadata = payload.get("metadata", {})
        if isinstance(metadata, dict) and float(metadata.get("duration_seconds", 0)) > 20:
            limitations.append("视频较长，系统最多抽取 120 帧；结果不代表整段动作。")
        result: dict[str, object] = {
            "assessment_id": assessment.id,
            "overall_score": assessment.overall_score,
            "dimension_scores": assessment.dimension_scores,
            "coverage": assessment.coverage,
            "confidence": confidence,
            "safety_mode": assessment.safety_mode,
            "safety_reasons": list(assessment.safety_reasons),
            "viewpoints": [item.value for item in assessment.viewpoints],
            "quality": assessment.quality,
            "findings": list(assessment.findings),
            "recommendations": list(assessment.recommendations),
            "limitations": limitations,
            "metrics": {
                key: {"value": metric.value, "unit": metric.unit,
                      "confidence": metric.confidence, "visibility": metric.visibility}
                for key, metric in artifacts.metrics.items()
            },
            "keyframes_available": len(artifacts.keyframes),
            "contact_sheet_available": artifacts.contact_sheet is not None,
        }
        result["report_markdown"] = render_running_form_report(result)
        payload["status"] = "completed"
        payload["assessment"] = result
        _save(_job_path(runtime, job_id), payload)
    except Exception as exc:
        allowed = {
            "pose_backend_unavailable", "corrupt_video", "video_too_long",
            "no_decodable_frames", "no_pose_detected",
        }
        payload["status"] = "failed"
        payload["error_code"] = str(exc) if str(exc) in allowed else "analysis_failed"
        _save(_job_path(runtime, job_id), payload)
    finally:
        try:
            if estimator is not None:
                estimator.close()
        finally:
            video_path.unlink(missing_ok=True)
