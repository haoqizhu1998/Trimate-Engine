"""Representative running segment selection."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean

from athlete_growth.adapters.running_form.contracts import PoseFrame
from athlete_growth.adapters.running_form.gait import GaitCycle, detect_gait_cycles


@dataclass(frozen=True)
class RunningSegment:
    start_ms: int
    end_ms: int
    score: float
    cycle_count: int
    reasons: tuple[str, ...]


def _group_cycles(
    cycles: list[GaitCycle],
    *,
    max_gap_ms: int,
) -> list[list[GaitCycle]]:
    groups: list[list[GaitCycle]] = []
    for cycle in cycles:
        if not groups or cycle.start_ms - groups[-1][-1].end_ms > max_gap_ms:
            groups.append([cycle])
        else:
            groups[-1].append(cycle)
    return groups


def select_running_segments(
    frames: list[PoseFrame],
    *,
    min_cycles: int = 3,
    max_segments: int = 3,
    max_gap_ms: int = 1200,
) -> list[RunningSegment]:
    cycles = detect_gait_cycles(frames)
    groups = _group_cycles(cycles, max_gap_ms=max_gap_ms)
    segments: list[RunningSegment] = []
    for group in groups:
        if len(group) < min_cycles:
            continue
        start = group[0].start_ms
        end = group[-1].end_ms
        selected_frames = [
            frame for frame in frames if start <= frame.timestamp_ms <= end
        ]
        visibility = (
            fmean(
                landmark.visibility
                for frame in selected_frames
                for landmark in (frame.landmarks[23], frame.landmarks[24],
                                 frame.landmarks[27], frame.landmarks[28])
            )
            if selected_frames
            else 0.0
        )
        score = len(group) * 10 + visibility
        segments.append(
            RunningSegment(
                start_ms=start,
                end_ms=end,
                score=round(score, 3),
                cycle_count=len(group),
                reasons=("complete_gait_cycles", "stable_pose_visibility"),
            )
        )
    return sorted(segments, key=lambda item: item.score, reverse=True)[
        :max_segments
    ]
