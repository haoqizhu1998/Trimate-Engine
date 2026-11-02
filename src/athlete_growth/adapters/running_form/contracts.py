"""Typed contracts shared by running-form analysis services."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Viewpoint(StrEnum):
    SIDE = "side"
    FRONT = "front"
    REAR = "rear"
    OBLIQUE = "oblique"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class AnalysisConfidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class JobStatus(StrEnum):
    RECEIVED = "received"
    PREPROCESSING = "preprocessing"
    POSE = "pose"
    SCORING = "scoring"
    REPORTING = "reporting"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class PoseLandmark:
    x: float
    y: float
    z: float = 0.0
    visibility: float = 1.0
    presence: float = 1.0


@dataclass(frozen=True)
class VideoFrame:
    timestamp_ms: int
    pixels: Any


@dataclass(frozen=True)
class PoseFrame:
    timestamp_ms: int
    landmarks: tuple[PoseLandmark, ...]


@dataclass(frozen=True)
class MetricResult:
    key: str
    value: float | None
    unit: str | None
    visibility: float
    confidence: float
    evidence_frames: tuple[int, ...] = ()


@dataclass(frozen=True)
class DimensionScore:
    key: str
    score: float | None
    weight: float
    measured_metrics: tuple[str, ...] = ()


@dataclass(frozen=True)
class SafetyDecision:
    safety_mode: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunningFormAssessment:
    id: str
    user_id: str
    status: JobStatus
    rule_version: str
    service_version: str
    viewpoints: tuple[Viewpoint, ...]
    coverage: float
    confidence: AnalysisConfidence
    dimension_scores: dict[str, float | None]
    overall_score: float | None
    safety_mode: bool
    safety_reasons: tuple[str, ...] = ()
    findings: tuple[dict[str, Any], ...] = ()
    recommendations: tuple[dict[str, Any], ...] = ()
    quality: dict[str, Any] = field(default_factory=dict)
    report_markdown: str | None = None
    comparison_assessment_id: str | None = None


@dataclass(frozen=True)
class RunningFormJob:
    id: str
    user_id: str
    idempotency_key: str
    status: JobStatus
    channel: str
    account_id: str
    peer_id: str
    session_key: str
    staged_media: tuple[str, ...]
    input_context: dict[str, Any]
    retry_count: int
    retry_at: str | None
    lease_token: str | None
    lease_expires_at: str | None
    delivery_status: str
    cleanup_status: str
    cleanup_retry_count: int
    cleanup_completed_at: str | None
    last_error_code: str | None


@dataclass(frozen=True)
class JobCreateResult:
    job: RunningFormJob
    created: bool


@dataclass(frozen=True)
class KeyframeRecord:
    id: str
    assessment_id: str
    metric_key: str | None
    relative_path: str
    sha256: str
    severity: str
