"""Pose-estimation protocol and backend-independent validation."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from athlete_growth.adapters.running_form.contracts import PoseFrame, VideoFrame


class PoseEstimator(Protocol):
    def estimate(self, frames: Iterable[VideoFrame]) -> list[PoseFrame]: ...


def validate_pose_frames(frames: Iterable[PoseFrame]) -> None:
    previous_timestamp = -1
    for frame in frames:
        if len(frame.landmarks) != 33:
            raise ValueError("pose frame must contain 33 landmarks")
        if frame.timestamp_ms < previous_timestamp:
            raise ValueError("pose frame timestamps must be monotonic")
        previous_timestamp = frame.timestamp_ms
