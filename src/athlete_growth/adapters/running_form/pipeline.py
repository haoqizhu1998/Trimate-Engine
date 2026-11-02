"""Local video-to-structured-assessment pipeline."""

# pyright: reportMissingImports=false

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from athlete_growth.adapters.running_form.annotations import (
    AnnotatedKeyframe,
    AnnotationFinding,
    annotate_keyframes,
    build_contact_sheet,
)
from athlete_growth.adapters.running_form.contracts import (
    AnalysisConfidence,
    JobStatus,
    MetricResult,
    RunningFormAssessment,
    RunningFormJob,
    Viewpoint,
)
from athlete_growth.adapters.running_form.media import VideoMediaService, sample_video_frames
from athlete_growth.adapters.running_form.metrics import compute_running_form_metrics
from athlete_growth.adapters.running_form.pose import PoseEstimator
from athlete_growth.adapters.running_form.quality import assess_pose_quality
from athlete_growth.adapters.running_form.rules import DIMENSION_METRICS
from athlete_growth.adapters.running_form.safety import evaluate_safety
from athlete_growth.adapters.running_form.scoring import score_running_form

SERVICE_VERSION = "running-form-service-v2"

DIMENSION_CONTENT = {
    "posture": (
        "头部与躯干控制可更稳定",
        "目视前方，躯干稳定；需要前倾时从脚踝自然微倾，不要弯腰",
        ["靠墙前倾跑 3组×20秒"],
    ),
    "braking": (
        "落地点与制动需要关注",
        "缩短向前够地，让脚更接近身体下方落地；不要强改前后掌着地",
        ["原地快步 3组×20秒", "A Skip 3组×20米"],
    ),
    "symmetry": (
        "左右稳定性存在可见差异",
        "保持骨盆朝前稳定，不要为追求对称强行改动作",
        ["单腿提踵 3组×12次"],
    ),
    "coordination": (
        "上下肢协调可以更放松",
        "肩手放松，手臂自然前后摆，避免明显横过身体中线",
        ["原地摆臂 3组×30秒"],
    ),
    "economy": (
        "垂直起伏或节奏稳定性可改善",
        "把力量送向前方，步频只和你自己的同配速基线比较",
        ["轻快跑 4组×30秒"],
    ),
}

METRIC_LABELS = {
    "trunk_lean_deg": "躯干相对垂直角",
    "pelvic_drop_deg": "骨盆可见倾斜角",
    "foot_strike_offset": "脚踝与髋部投影偏移",
    "tibial_angle_deg": "落地侧小腿角度",
    "left_right_asymmetry": "左右动作幅度差异",
    "knee_track_ratio": "膝部轨迹偏移",
    "arm_crossing_ratio": "摆臂越过身体中线比例",
    "vertical_oscillation_ratio": "垂直起伏比例",
    "cadence_variability": "步态周期波动",
}

CHANGE_GUARDRAIL = (
    "一次只改一个动作变量，从轻松跑短段开始；如果小腿、跟腱、足底或膝部"
    "出现新不适，停止该调整并回到原有自然动作。"
)


@dataclass(frozen=True)
class PipelineArtifacts:
    assessment: RunningFormAssessment
    metrics: dict[str, MetricResult]
    keyframes: tuple[AnnotatedKeyframe, ...]
    contact_sheet: Path | None
    limitations: tuple[str, ...]


class LocalRunningFormPipeline:
    def __init__(
        self,
        *,
        estimator: PoseEstimator,
        media_service: VideoMediaService,
        max_frames_per_video: int = 120,
    ) -> None:
        self.estimator = estimator
        self.media_service = media_service
        self.max_frames_per_video = max_frames_per_video

    def analyze(
        self,
        job: RunningFormJob,
        *,
        user_directory: Path,
    ) -> PipelineArtifacts:
        all_metrics: dict[str, MetricResult] = {}
        all_images: dict[int, Image.Image] = {}
        all_poses = {}
        viewpoints: list[Viewpoint] = []
        usable_dimensions: set[str] = set()
        quality_rows = []
        timestamp_offset = 0

        for relative in job.staged_media:
            video_path = (user_directory / relative).resolve()
            if not video_path.is_relative_to(user_directory.resolve()):
                raise ValueError("staged_media_outside_user_directory")
            metadata = self.media_service.probe_video(video_path)
            if not metadata.usable:
                raise ValueError(metadata.error_code or "unusable_video")
            video_frames = sample_video_frames(
                video_path,
                max_frames=self.max_frames_per_video,
                timestamp_offset_ms=timestamp_offset,
            )
            poses = self.estimator.estimate(video_frames)
            quality = assess_pose_quality(poses)
            quality_rows.append(quality)
            viewpoints.append(quality.viewpoint)
            usable_dimensions.update(quality.usable_metrics_mask)
            current_metrics = compute_running_form_metrics(
                poses, viewpoint=quality.viewpoint
            )
            for key, metric in current_metrics.items():
                existing = all_metrics.get(key)
                if existing is None or metric.confidence > existing.confidence:
                    all_metrics[key] = metric
            for frame in video_frames:
                all_images[frame.timestamp_ms] = Image.fromarray(frame.pixels)
            all_poses.update({frame.timestamp_ms: frame for frame in poses})
            timestamp_offset += max(frame.timestamp_ms for frame in video_frames) + 1000

        if not all_poses:
            raise ValueError("no_pose_detected")
        safety = evaluate_safety(
            all_metrics,
            user_note=str(job.input_context.get("note") or ""),
        )
        scoring = score_running_form(
            all_metrics,
            usable_dimensions=usable_dimensions,
            safety_decision=safety,
        )
        findings, recommendations, annotations = _build_findings(
            scoring.dimension_scores, all_metrics
        )
        assessment_id = job.id.replace("rfj_", "rfa_", 1)
        quality_score = sum(item.quality_score for item in quality_rows) / len(quality_rows)
        confidence = (
            AnalysisConfidence.HIGH
            if scoring.confidence >= 0.8
            else AnalysisConfidence.MEDIUM
            if scoring.confidence >= 0.6
            else AnalysisConfidence.LOW
        )
        unique_viewpoints = tuple(dict.fromkeys(viewpoints))
        limitations = _build_limitations(
            scoring.dimension_scores,
            viewpoints=unique_viewpoints,
            quality_reasons=(
                reason
                for quality in quality_rows
                for reason in quality.reasons
            ),
        )
        assessment = RunningFormAssessment(
            id=assessment_id,
            user_id=job.user_id,
            status=JobStatus.REPORTING,
            rule_version=scoring.rule_version,
            service_version=SERVICE_VERSION,
            viewpoints=unique_viewpoints,
            coverage=scoring.coverage,
            confidence=confidence,
            dimension_scores=scoring.dimension_scores,
            overall_score=scoring.overall_score,
            safety_mode=safety.safety_mode,
            safety_reasons=safety.reasons,
            findings=tuple(findings),
            recommendations=tuple(recommendations),
            quality={
                "quality_score": round(quality_score, 3),
                "video_count": len(job.staged_media),
                "capture_guidance": (
                    "复测时固定相机并拍到全身和双脚；侧面用于落地点与制动，"
                    "正面或后方用于左右稳定性。"
                ),
            },
        )

        keyframe_dir = user_directory / "running-form" / "keyframes" / assessment_id
        keyframes = tuple(
            annotate_keyframes(
                images=all_images,
                poses=all_poses,
                findings=annotations,
                output_dir=keyframe_dir,
            )
        )
        contact_sheet = None
        if keyframes:
            contact_sheet = build_contact_sheet(
                list(keyframes),
                user_directory / "running-form" / "reports" / f"{assessment_id}.png",
            )
        return PipelineArtifacts(
            assessment=assessment,
            metrics=all_metrics,
            keyframes=keyframes,
            contact_sheet=contact_sheet,
            limitations=limitations,
        )


def _build_findings(
    scores: dict[str, float | None],
    metrics: dict[str, MetricResult],
) -> tuple[list[dict], list[dict], list[AnnotationFinding]]:
    ranked = sorted(
        (
            (key, score)
            for key, score in scores.items()
            if score is not None and score < 85
        ),
        key=lambda item: item[1],
    )[:3]
    findings = []
    recommendations = []
    annotations = []
    for dimension, score in ranked:
        title, cue, drills = DIMENSION_CONTENT[dimension]
        metric = next(
            (metrics[key] for key in DIMENSION_METRICS[dimension] if key in metrics),
            None,
        )
        evidence = _format_metric_evidence(metric)
        severity = "priority" if score < 55 else "improve"
        findings.append(
            {"key": dimension, "title": title, "evidence": evidence, "severity": severity}
        )
        recommendations.append(
            {
                "problem_key": dimension,
                "cue": cue,
                "drills": drills,
                "frequency": "每周2次",
                "caution": CHANGE_GUARDRAIL,
            }
        )
        if metric and metric.evidence_frames:
            annotations.append(
                AnnotationFinding(
                    key=dimension,
                    severity=severity,
                    label=title,
                    evidence_timestamp_ms=metric.evidence_frames[0],
                )
            )
    return findings, recommendations, annotations


def _format_metric_evidence(metric: MetricResult | None) -> str:
    if metric is None or metric.value is None:
        return "本次视频中的可见动作指标"
    label = METRIC_LABELS.get(metric.key, metric.key)
    if metric.unit == "deg":
        value = f"{float(metric.value):.1f}°"
    elif metric.unit in {"ratio", "body_ratio"}:
        value = f"{float(metric.value) * 100:.1f}%"
    else:
        value = str(metric.value)
    return f"{label} {value}（二维视频估算）"


def _build_limitations(
    scores: dict[str, float | None],
    *,
    viewpoints: tuple[Viewpoint, ...],
    quality_reasons: Iterable[str],
) -> tuple[str, ...]:
    items: list[str] = []
    viewpoint_set = set(viewpoints)
    if scores.get("braking") is None and Viewpoint.SIDE not in viewpoint_set:
        items.append("当前没有可靠纯侧面视角，落地点相对身体位置与制动倾向未评估。")
    if scores.get("symmetry") is None and not viewpoint_set.intersection(
        {Viewpoint.FRONT, Viewpoint.REAR}
    ):
        items.append("当前没有可靠正面或后方视角，左右对称、膝部轨迹与骨盆稳定性未评估。")

    labels = {
        "posture": "姿态控制",
        "braking": "落地与制动",
        "symmetry": "稳定与左右对称",
        "coordination": "动作协调性",
        "economy": "跑步经济性倾向",
    }
    covered = {"braking", "symmetry"}
    for dimension, score in scores.items():
        if score is None and dimension not in covered:
            items.append(f"{labels[dimension]}因视角或可见度不足，未评估。")

    reason_labels = {
        "low_landmark_visibility": "人体关键点可见度偏低，宽松衣物、遮挡或光线可能影响结果。",
        "multiple_people": "画面中出现多人，无法可靠锁定单一跑者。",
    }
    for reason in quality_reasons:
        message = reason_labels.get(reason)
        if message:
            items.append(message)
    return tuple(dict.fromkeys(items))
