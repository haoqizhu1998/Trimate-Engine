"""Deterministic gait event and cycle detection."""

from __future__ import annotations

from dataclasses import dataclass

from athlete_growth.adapters.running_form.contracts import PoseFrame


@dataclass(frozen=True)
class GaitCycle:
    start_ms: int
    end_ms: int
    side: str
    opposite_contact_ms: int | None


def _contact_times(
    frames: list[PoseFrame],
    landmark_index: int,
    *,
    min_amplitude: float,
) -> list[int]:
    usable = [
        (frame.timestamp_ms, frame.landmarks[landmark_index].y)
        for frame in frames
        if frame.landmarks[landmark_index].visibility >= 0.5
    ]
    if len(usable) < 5:
        return []
    values = [value for _, value in usable]
    if max(values) - min(values) < min_amplitude:
        return []
    return [
        usable[index][0]
        for index in range(1, len(usable) - 1)
        if usable[index][1] > usable[index - 1][1]
        and usable[index][1] >= usable[index + 1][1]
    ]


def detect_gait_cycles(
    frames: list[PoseFrame],
    *,
    min_amplitude: float = 0.02,
    min_cycle_ms: int = 250,
    max_cycle_ms: int = 2000,
) -> list[GaitCycle]:
    if len(frames) < 5:
        return []
    ordered = sorted(frames, key=lambda frame: frame.timestamp_ms)
    left_contacts = _contact_times(
        ordered, 27, min_amplitude=min_amplitude
    )
    right_contacts = _contact_times(
        ordered, 28, min_amplitude=min_amplitude
    )
    cycles: list[GaitCycle] = []
    for start, end in zip(left_contacts, left_contacts[1:], strict=False):
        duration = end - start
        if duration < min_cycle_ms or duration > max_cycle_ms:
            continue
        opposite = next(
            (value for value in right_contacts if start < value < end),
            None,
        )
        if opposite is None:
            continue
        cycles.append(
            GaitCycle(
                start_ms=start,
                end_ms=end,
                side="left",
                opposite_contact_ms=opposite,
            )
        )
    return cycles
