"""Versioned deterministic scoring rules for running form."""

from __future__ import annotations

RULE_VERSION = "running-form-rules-v2"

DIMENSION_WEIGHTS = {
    "posture": 20.0,
    "braking": 25.0,
    "symmetry": 20.0,
    "coordination": 15.0,
    "economy": 20.0,
}

DIMENSION_METRICS = {
    "posture": ("trunk_lean_deg", "pelvic_drop_deg"),
    "braking": ("foot_strike_offset", "tibial_angle_deg"),
    "symmetry": (
        "left_right_asymmetry",
        "pelvic_drop_deg",
        "knee_track_ratio",
    ),
    "coordination": ("arm_crossing_ratio", "cadence_variability"),
    "economy": ("vertical_oscillation_ratio", "cadence_variability"),
}

METRIC_TARGETS = {
    # Upright and a small whole-body lean can both be economical. Do not punish
    # an otherwise stable runner for failing to reach a prescribed lean angle.
    "trunk_lean_deg": (0.0, 15.0, 25.0),
    "pelvic_drop_deg": (0.0, 5.0, 15.0),
    "foot_strike_offset": (0.0, 0.08, 0.25),
    "tibial_angle_deg": (0.0, 8.0, 25.0),
    "left_right_asymmetry": (0.0, 0.1, 0.4),
    "knee_track_ratio": (0.0, 0.15, 0.5),
    "arm_crossing_ratio": (0.0, 0.2, 0.7),
    "vertical_oscillation_ratio": (0.0, 0.08, 0.2),
    "cadence_variability": (0.0, 0.1, 0.3),
}

MIN_TOTAL_COVERAGE = 0.4
