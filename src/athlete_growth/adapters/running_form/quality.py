"""Viewpoint classification and pose quality gates."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean, median

from athlete_growth.adapters.running_form.contracts import PoseFrame, Viewpoint


@dataclass(frozen=True)
class PoseQualityAssessment:
    viewpoint: Viewpoint
    quality_score: float
    usable: bool
    usable_metrics_mask: frozenset[str]
    reasons: tuple[str, ...]


def assess_pose_quality(
    frames: list[PoseFrame],
    *,
    person_counts: list[int] | None = None,
) -> PoseQualityAssessment:
    if not frames:
        return PoseQualityAssessment(
            viewpoint=Viewpoint.UNKNOWN,
            quality_score=0.0,
            usable=False,
            usable_metrics_mask=frozenset(),
            reasons=("no_pose_detected",),
        )

    widths = []
    nose_visibility = []
    visible_values = []
    for frame in frames:
        landmarks = frame.landmarks
        widths.append(
            (
                abs(landmarks[11].x - landmarks[12].x)
                + abs(landmarks[23].x - landmarks[24].x)
            )
            / 2
        )
        nose_visibility.append(landmarks[0].visibility)
        visible_values.extend(item.visibility for item in landmarks)

    projected_width = median(widths)
    face_visibility = median(nose_visibility)
    if projected_width <= 0.1:
        viewpoint = Viewpoint.SIDE
        metric_mask = {"posture", "braking", "coordination", "economy"}
    elif projected_width >= 0.22:
        viewpoint = Viewpoint.FRONT if face_visibility >= 0.5 else Viewpoint.REAR
        metric_mask = {"posture", "symmetry", "coordination", "economy"}
    else:
        viewpoint = Viewpoint.OBLIQUE
        metric_mask = {"posture", "coordination", "economy"}

    average_visibility = fmean(visible_values)
    reasons: list[str] = []
    if average_visibility < 0.5:
        reasons.append("low_landmark_visibility")
    if person_counts and any(count > 1 for count in person_counts):
        reasons.append("multiple_people")
    usable = not reasons
    if not usable:
        metric_mask = set()
    return PoseQualityAssessment(
        viewpoint=viewpoint,
        quality_score=round(max(0.0, min(1.0, average_visibility)), 3),
        usable=usable,
        usable_metrics_mask=frozenset(metric_mask),
        reasons=tuple(reasons),
    )
