"""Annotated evidence keyframes and a single-delivery contact sheet."""

# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from athlete_growth.adapters.running_form.contracts import PoseFrame

SKELETON_CONNECTIONS = (
    (11, 12),
    (11, 13),
    (13, 15),
    (12, 14),
    (14, 16),
    (11, 23),
    (12, 24),
    (23, 24),
    (23, 25),
    (25, 27),
    (24, 26),
    (26, 28),
)

SEVERITY_COLORS = {
    "normal": "#43e58f",
    "improve": "#ffd34d",
    "priority": "#ff5d68",
    "risk": "#ff5d68",
}

FONT_CANDIDATES = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


@dataclass(frozen=True)
class AnnotationFinding:
    key: str
    severity: str
    label: str
    evidence_timestamp_ms: int


@dataclass(frozen=True)
class AnnotatedKeyframe:
    metric_key: str
    severity: str
    label: str
    path: Path
    sha256: str


def _font(size: int) -> ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _draw_pose(
    image: Image.Image,
    pose: PoseFrame,
    finding: AnnotationFinding,
) -> None:
    draw = ImageDraw.Draw(image, "RGBA")
    width, height = image.size
    color = SEVERITY_COLORS.get(finding.severity, "#ffd34d")
    for first, second in SKELETON_CONNECTIONS:
        start = pose.landmarks[first]
        end = pose.landmarks[second]
        if min(start.visibility, end.visibility) < 0.5:
            continue
        draw.line(
            (start.x * width, start.y * height, end.x * width, end.y * height),
            fill=color,
            width=max(3, width // 160),
        )
    for landmark in pose.landmarks:
        if landmark.visibility < 0.5:
            continue
        x, y = landmark.x * width, landmark.y * height
        radius = max(3, width // 180)
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)
    draw.rounded_rectangle(
        (20, 20, min(width - 20, 360), 78),
        radius=12,
        fill="#05192bdd",
        outline=color,
        width=2,
    )
    draw.text((36, 34), finding.label, fill="#f4f8fa", font=_font(26))


def annotate_keyframes(
    *,
    images: dict[int, Image.Image],
    poses: dict[int, PoseFrame],
    findings: list[AnnotationFinding],
    output_dir: Path,
) -> list[AnnotatedKeyframe]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    results: list[AnnotatedKeyframe] = []
    for finding in findings[:3]:
        timestamp = finding.evidence_timestamp_ms
        if timestamp not in images or timestamp not in poses:
            continue
        image = images[timestamp].convert("RGB").copy()
        _draw_pose(image, poses[timestamp], finding)
        path = output_dir / f"evidence-{len(results) + 1}-{finding.key}.png"
        image.save(path, format="PNG", optimize=True)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        results.append(
            AnnotatedKeyframe(
                metric_key=finding.key,
                severity=finding.severity,
                label=finding.label,
                path=path,
                sha256=digest,
            )
        )
    return results


def build_contact_sheet(
    keyframes: list[AnnotatedKeyframe],
    output_path: Path,
) -> Path:
    if not keyframes or len(keyframes) > 3:
        raise ValueError("contact sheet requires one to three keyframes")
    canvas = Image.new("RGB", (1080, 1440), "#031522")
    draw = ImageDraw.Draw(canvas)
    draw.text((54, 42), "RUNNING FORM", fill="#f4f8fa", font=_font(46))
    draw.text((54, 100), "KEY MOMENTS", fill="#35e7c2", font=_font(28))
    available_height = 1260
    panel_height = available_height // len(keyframes)
    for index, keyframe in enumerate(keyframes):
        top = 150 + index * panel_height
        with Image.open(keyframe.path) as source:
            source = source.convert("RGB")
            source.thumbnail((972, panel_height - 72), Image.Resampling.LANCZOS)
            left = (1080 - source.width) // 2
            canvas.paste(source, (left, top + 48))
        color = SEVERITY_COLORS.get(keyframe.severity, "#ffd34d")
        draw.text(
            (54, top + 8),
            f"{index + 1:02d}  {keyframe.label}",
            fill=color,
            font=_font(26),
        )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG", optimize=True)
    return output_path
