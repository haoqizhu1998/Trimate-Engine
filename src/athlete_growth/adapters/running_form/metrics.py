"""Explainable 2D metrics derived from pose landmarks."""

from __future__ import annotations

import math
from statistics import fmean, median

from athlete_growth.adapters.running_form.contracts import (
    MetricResult,
    PoseFrame,
    Viewpoint,
)
from athlete_growth.adapters.running_form.gait import detect_gait_cycles


def _midpoint(frame: PoseFrame, left: int, right: int) -> tuple[float, float]:
    first = frame.landmarks[left]
    second = frame.landmarks[right]
    return ((first.x + second.x) / 2, (first.y + second.y) / 2)


def _angle_from_vertical(
    upper: tuple[float, float], lower: tuple[float, float]
) -> float:
    dx = lower[0] - upper[0]
    dy = lower[1] - upper[1]
    return abs(math.degrees(math.atan2(dx, dy)))


def _visible(frame: PoseFrame, *indices: int, threshold: float = 0.5) -> bool:
    return all(frame.landmarks[index].visibility >= threshold for index in indices)


def _metric(
    key: str,
    values: list[tuple[int, float]],
    *,
    unit: str,
    total_frames: int,
) -> MetricResult | None:
    if not values:
        return None
    representative = max(values, key=lambda item: abs(item[1]))
    visibility = len(values) / max(1, total_frames)
    return MetricResult(
        key=key,
        value=round(median(value for _, value in values), 4),
        unit=unit,
        visibility=round(visibility, 3),
        confidence=round(min(1.0, visibility * 0.95), 3),
        evidence_frames=(representative[0],),
    )


def compute_running_form_metrics(
    frames: list[PoseFrame],
    *,
    viewpoint: Viewpoint,
) -> dict[str, MetricResult]:
    metrics: dict[str, MetricResult] = {}
    if not frames:
        return metrics

    trunk_values: list[tuple[int, float]] = []
    pelvic_values: list[tuple[int, float]] = []
    foot_values: list[tuple[int, float]] = []
    tibial_values: list[tuple[int, float]] = []
    knee_values: list[tuple[int, float]] = []
    arm_crossing = 0
    arm_visible = 0
    hip_y_values: list[float] = []
    body_heights: list[float] = []

    for frame in frames:
        if _visible(frame, 11, 12, 23, 24):
            shoulders = _midpoint(frame, 11, 12)
            hips = _midpoint(frame, 23, 24)
            trunk_values.append(
                (frame.timestamp_ms, _angle_from_vertical(shoulders, hips))
            )
            hip_y_values.append(hips[1])
            if _visible(frame, 27, 28):
                ankle_y = max(frame.landmarks[27].y, frame.landmarks[28].y)
                body_heights.append(abs(ankle_y - shoulders[1]))

        if _visible(frame, 23, 24):
            left_hip = frame.landmarks[23]
            right_hip = frame.landmarks[24]
            pelvic_values.append(
                (
                    frame.timestamp_ms,
                    abs(
                        math.degrees(
                            math.atan2(
                                right_hip.y - left_hip.y,
                                right_hip.x - left_hip.x,
                            )
                        )
                    ),
                )
            )

        if viewpoint is Viewpoint.SIDE and _visible(frame, 23, 25, 27, 28):
            left_visibility = frame.landmarks[27].visibility
            right_visibility = frame.landmarks[28].visibility
            ankle_index = 27 if left_visibility >= right_visibility else 28
            hip_index = 23 if ankle_index == 27 else 24
            knee_index = 25 if ankle_index == 27 else 26
            ankle = frame.landmarks[ankle_index]
            hip = frame.landmarks[hip_index]
            knee = frame.landmarks[knee_index]
            foot_values.append((frame.timestamp_ms, abs(ankle.x - hip.x)))
            tibial_values.append(
                (
                    frame.timestamp_ms,
                    _angle_from_vertical((knee.x, knee.y), (ankle.x, ankle.y)),
                )
            )

        if viewpoint in {Viewpoint.FRONT, Viewpoint.REAR} and _visible(
            frame, 23, 24, 25, 26, 27, 28
        ):
            hip_width = abs(frame.landmarks[24].x - frame.landmarks[23].x)
            if hip_width > 0.001:
                left_track = abs(
                    frame.landmarks[25].x
                    - (frame.landmarks[23].x + frame.landmarks[27].x) / 2
                )
                right_track = abs(
                    frame.landmarks[26].x
                    - (frame.landmarks[24].x + frame.landmarks[28].x) / 2
                )
                knee_values.append(
                    (frame.timestamp_ms, (left_track + right_track) / hip_width)
                )

        if _visible(frame, 15, 16, 23, 24):
            center_x = _midpoint(frame, 23, 24)[0]
            arm_visible += 1
            if (
                frame.landmarks[15].x > center_x
                or frame.landmarks[16].x < center_x
            ):
                arm_crossing += 1

    for key, values, unit in (
        ("trunk_lean_deg", trunk_values, "deg"),
        ("pelvic_drop_deg", pelvic_values, "deg"),
        ("foot_strike_offset", foot_values, "body_ratio"),
        ("tibial_angle_deg", tibial_values, "deg"),
        ("knee_track_ratio", knee_values, "ratio"),
    ):
        result = _metric(key, values, unit=unit, total_frames=len(frames))
        if result is not None:
            metrics[key] = result

    if viewpoint in {Viewpoint.FRONT, Viewpoint.REAR}:
        left_y = [frame.landmarks[27].y for frame in frames if _visible(frame, 27)]
        right_y = [frame.landmarks[28].y for frame in frames if _visible(frame, 28)]
        if len(left_y) >= 2 and len(right_y) >= 2:
            left_range = max(left_y) - min(left_y)
            right_range = max(right_y) - min(right_y)
            denominator = max(0.001, (left_range + right_range) / 2)
            metrics["left_right_asymmetry"] = MetricResult(
                key="left_right_asymmetry",
                value=round(abs(left_range - right_range) / denominator, 4),
                unit="ratio",
                visibility=min(len(left_y), len(right_y)) / len(frames),
                confidence=0.85,
                evidence_frames=(frames[-1].timestamp_ms,),
            )

    if arm_visible:
        metrics["arm_crossing_ratio"] = MetricResult(
            key="arm_crossing_ratio",
            value=round(arm_crossing / arm_visible, 4),
            unit="ratio",
            visibility=arm_visible / len(frames),
            confidence=0.8,
            evidence_frames=(frames[0].timestamp_ms,),
        )

    if hip_y_values and body_heights and median(body_heights) > 0.01:
        oscillation = (max(hip_y_values) - min(hip_y_values)) / median(body_heights)
        metrics["vertical_oscillation_ratio"] = MetricResult(
            key="vertical_oscillation_ratio",
            value=round(oscillation, 4),
            unit="body_ratio",
            visibility=len(hip_y_values) / len(frames),
            confidence=0.8,
            evidence_frames=(frames[-1].timestamp_ms,),
        )

    cycles = detect_gait_cycles(frames)
    durations = [cycle.end_ms - cycle.start_ms for cycle in cycles]
    if len(durations) >= 2 and fmean(durations) > 0:
        mean_duration = fmean(durations)
        variability = (
            fmean(abs(value - mean_duration) for value in durations) / mean_duration
        )
        metrics["cadence_variability"] = MetricResult(
            key="cadence_variability",
            value=round(variability, 4),
            unit="ratio",
            visibility=1.0,
            confidence=0.85,
            evidence_frames=(cycles[0].start_ms,),
        )
    return metrics
