"""Lazy MediaPipe Pose Landmarker adapter."""

# pyright: reportMissingImports=false

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from athlete_growth.adapters.running_form.contracts import (
    PoseFrame,
    PoseLandmark,
    VideoFrame,
)
from athlete_growth.adapters.running_form.pose import validate_pose_frames


class MediaPipePoseEstimator:
    def __init__(
        self,
        landmarker: Any,
        *,
        image_factory: Callable[[Any], Any],
    ) -> None:
        self.landmarker = landmarker
        self.image_factory = image_factory

    @classmethod
    def from_model_path(cls, model_path: Path) -> MediaPipePoseEstimator:
        try:
            import mediapipe as mp
        except ImportError as exc:
            raise RuntimeError("pose_backend_unavailable") from exc

        # LaunchAgents do not have a reliable macOS Metal service, so the
        # MediaPipe GPU delegate can abort the whole worker before Python can
        # report an error. Running-form jobs are short enough for CPU inference
        # and this keeps the background worker deterministic.
        base_options = mp.tasks.BaseOptions(
            model_asset_path=str(model_path),
            delegate=mp.tasks.BaseOptions.Delegate.CPU,
        )
        options = mp.tasks.vision.PoseLandmarkerOptions(
            base_options=base_options,
            running_mode=mp.tasks.vision.RunningMode.VIDEO,
            num_poses=1,
            min_pose_detection_confidence=0.5,
            min_pose_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        landmarker = mp.tasks.vision.PoseLandmarker.create_from_options(options)

        def image_factory(pixels: Any) -> Any:
            return mp.Image(image_format=mp.ImageFormat.SRGB, data=pixels)

        return cls(landmarker, image_factory=image_factory)

    def estimate(self, frames: Iterable[VideoFrame]) -> list[PoseFrame]:
        output: list[PoseFrame] = []
        for frame in frames:
            result = self.landmarker.detect_for_video(
                self.image_factory(frame.pixels), frame.timestamp_ms
            )
            if not result.pose_landmarks:
                continue
            landmarks = tuple(
                PoseLandmark(
                    x=float(item.x),
                    y=float(item.y),
                    z=float(item.z),
                    visibility=float(item.visibility or 0.0),
                    presence=float(item.presence or 0.0),
                )
                for item in result.pose_landmarks[0]
            )
            output.append(
                PoseFrame(timestamp_ms=frame.timestamp_ms, landmarks=landmarks)
            )
        validate_pose_frames(output)
        return output

    def close(self) -> None:
        self.landmarker.close()
