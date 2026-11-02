"""Secure local video handling for running-form analysis."""

# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athlete_growth.adapters.running_form.contracts import VideoFrame

SUPPORTED_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm"}


class MediaValidationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class MediaLimits:
    max_bytes: int = 500 * 1024 * 1024
    max_duration_seconds: float = 600.0


@dataclass(frozen=True)
class VideoMetadata:
    duration_seconds: float
    fps: float
    frame_count: int
    width: int
    height: int
    rotation: int
    usable: bool = True
    error_code: str | None = None


@dataclass(frozen=True)
class RuntimeStatus:
    available: bool
    error_code: str | None = None


def running_form_runtime_status(
    *,
    python_version: tuple[int, int] | None = None,
    module_finder: Callable[[str], Any] = importlib.util.find_spec,
) -> RuntimeStatus:
    version = python_version or sys.version_info[:2]
    if version < (3, 11) or version >= (3, 13):
        return RuntimeStatus(False, "unsupported_python")
    for module in ("cv2", "mediapipe", "PIL"):
        if module_finder(module) is None:
            return RuntimeStatus(False, "pose_backend_unavailable")
    return RuntimeStatus(True)


class VideoMediaService:
    def __init__(
        self,
        *,
        inbound_media_root: Path,
        users_root: Path,
        limits: MediaLimits | None = None,
    ) -> None:
        self.inbound_media_root = Path(inbound_media_root)
        self.users_root = Path(users_root)
        self.limits = limits or MediaLimits()

    def resolve_and_validate(self, ref: str) -> Path:
        prefix = "media://inbound/"
        if not ref.startswith(prefix):
            raise MediaValidationError("invalid_media_ref")
        relative = ref.removeprefix(prefix)
        if Path(relative).suffix.lower() not in SUPPORTED_VIDEO_SUFFIXES:
            raise MediaValidationError("unsupported_video_format")
        root = self.inbound_media_root.resolve()
        candidate = root / relative
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise MediaValidationError("invalid_media_ref") from exc
        if not resolved.is_relative_to(root) or candidate.is_symlink():
            raise MediaValidationError("invalid_media_ref")
        size = resolved.stat().st_size
        if size == 0:
            raise MediaValidationError("empty_video")
        if size > self.limits.max_bytes:
            raise MediaValidationError("video_too_large")
        return resolved

    def content_hash(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def stage_video(
        self,
        *,
        source: Path,
        user_directory: Path,
        job_id: str,
        index: int,
    ) -> str:
        users_root = self.users_root.resolve()
        user_directory = user_directory.resolve()
        if not user_directory.is_relative_to(users_root):
            raise MediaValidationError("invalid_user_directory")
        relative = Path("running-form") / "tmp" / job_id / (
            f"video-{index}{source.suffix.lower()}"
        )
        destination = user_directory / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(destination.parent, 0o700)
        with source.open("rb") as input_file, destination.open("xb") as output:
            shutil.copyfileobj(input_file, output, length=1024 * 1024)
        os.chmod(destination, 0o600)
        return relative.as_posix()

    def probe_video(
        self,
        path: Path,
        *,
        probe: Callable[[Path], dict[str, Any]] | None = None,
    ) -> VideoMetadata:
        values = (probe or _opencv_probe)(path)
        duration = float(values["duration_seconds"])
        if duration <= 0:
            return VideoMetadata(**values, usable=False, error_code="corrupt_video")
        if duration > self.limits.max_duration_seconds:
            return VideoMetadata(
                **values, usable=False, error_code="video_too_long"
            )
        return VideoMetadata(**values)


def _opencv_probe(path: Path) -> dict[str, Any]:
    try:
        import cv2
    except ImportError as exc:
        raise MediaValidationError("pose_backend_unavailable") from exc
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise MediaValidationError("corrupt_video")
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        rotation_property = getattr(cv2, "CAP_PROP_ORIENTATION_META", None)
        rotation = int(capture.get(rotation_property)) if rotation_property else 0
        duration = frame_count / fps if fps > 0 else 0.0
        return {
            "duration_seconds": duration,
            "fps": fps,
            "frame_count": frame_count,
            "width": width,
            "height": height,
            "rotation": rotation,
        }
    finally:
        capture.release()


def sample_video_frames(
    path: Path,
    *,
    max_frames: int = 120,
    target_fps: float = 8.0,
    timestamp_offset_ms: int = 0,
) -> list[VideoFrame]:
    """Decode a bounded frame sample without retaining the whole video."""

    try:
        import cv2
    except ImportError as exc:
        raise MediaValidationError("pose_backend_unavailable") from exc
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        capture.release()
        raise MediaValidationError("corrupt_video")
    source_fps = float(capture.get(cv2.CAP_PROP_FPS)) or target_fps
    stride = max(1, round(source_fps / target_fps))
    frames: list[VideoFrame] = []
    index = 0
    try:
        while len(frames) < max_frames:
            ok, pixels = capture.read()
            if not ok:
                break
            if index % stride == 0:
                rgb = cv2.cvtColor(pixels, cv2.COLOR_BGR2RGB)
                timestamp = round(index * 1000 / source_fps) + timestamp_offset_ms
                frames.append(VideoFrame(timestamp_ms=timestamp, pixels=rgb))
            index += 1
    finally:
        capture.release()
    if not frames:
        raise MediaValidationError("no_decodable_frames")
    return frames
