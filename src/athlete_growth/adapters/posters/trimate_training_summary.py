#!/usr/bin/env python3
"""Generate a shareable training-summary poster for OpenClaw media delivery."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

WIDTH = 1024
HEIGHT = 1536
DEFAULT_OUTBOUND_DIR = Path("~/.openclaw/media/outbound").expanduser()
ICON_ASSET_DIR = Path(
    os.environ.get(
        "TRIMATE_ICON_ASSET_DIR",
        str(Path(__file__).resolve().parent / "icons"),
    )
)

ACTIVITY_ICON_ASSETS = {
    "run": "running_green.png",
    "running": "running_green.png",
    "bike": "cycling_yellow.png",
    "cycling": "cycling_yellow.png",
    "swim": "swimming_blue.png",
    "swimming": "swimming_blue.png",
    "strength": "dumbbell_purple.png",
    "dumbbell": "dumbbell_purple.png",
    "rest": "meditation_cyan.png",
    "meditation": "meditation_cyan.png",
}

OUTLINE_ICON_ASSETS = {
    "bars": "bar_chart_white.png",
    "clock": "clock_green.png",
    "fire": "fire_orange.png",
    "mountain": "mountain_mint.png",
    "heart": "heart_red.png",
    "sleep": "moon_blue.png",
    "battery": "battery_green.png",
    "warning": "warning_red.png",
    "warning_yellow": "warning_yellow.png",
    "trend": "trend_arrow_white_green.png",
    "trend_circle": "trend_circle_green.png",
    "trophy": "trophy_yellow.png",
    "spark": "sparkles_yellow.png",
    "star": "star_yellow.png",
    "crown": "crown_yellow.png",
    "map_pin": "map_pin_cyan.png",
    "swoosh": "sport_swoosh_white.png",
}


@dataclass(frozen=True)
class Metric:
    label: str
    value: str
    subvalue: str = ""
    icon: str = ""
    color: str = "#c8ff47"


@dataclass(frozen=True)
class TimelineDay:
    day: str
    date: str
    title: str
    details: tuple[str, ...]
    tone: str
    icon: str = ""


@dataclass(frozen=True)
class PosterData:
    title: str
    period: str
    main_title: str
    subtitle: str
    date_range: str
    headline: str
    weekly_totals: tuple[Metric, ...]
    key_metrics: tuple[Metric, ...]
    highlight: str
    insights: str
    recommendations: tuple[str, ...]
    quote: str
    timeline: tuple[TimelineDay, ...]


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _fit_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _fit_by_width(text: str, max_chars: int) -> str:
    units = 0
    result = []
    for char in text:
        units += 2 if ord(char) > 127 else 1
        if units > max_chars:
            break
        result.append(char)
    fitted = "".join(result).rstrip()
    return fitted if fitted == text else fitted + "…"


def _wrapped_lines(text: str, max_chars: int, max_lines: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
        if len(lines) == max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    if len(lines) == max_lines and len(" ".join(words)) > len(" ".join(lines)):
        lines[-1] = _fit_text(lines[-1], max_chars)
    return lines


def _visual_units(text: str) -> int:
    return sum(2 if ord(char) > 127 else 1 for char in text)


def _wrap_by_width(text: str, max_units: int, max_lines: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in text:
        if char == "\n":
            if current:
                lines.append(current.rstrip())
                current = ""
            if len(lines) >= max_lines:
                return lines[:max_lines]
            continue
        candidate = f"{current}{char}"
        if current and _visual_units(candidate) > max_units:
            lines.append(current.rstrip())
            current = char.lstrip()
            if len(lines) >= max_lines:
                return lines[:max_lines]
            continue
        current = candidate
    if current and len(lines) < max_lines:
        lines.append(current.rstrip())
    return lines


def _svg_multiline_text(
    text: str,
    x: int,
    y: int,
    max_units: int,
    max_lines: int,
    line_height: int,
    *,
    font_size: float = 15,
    weight: int = 680,
    fill: str = "#F4F7FA",
) -> str:
    lines = _wrap_by_width(text, max_units, max_lines)
    rendered_lines = []
    for index, line in enumerate(lines):
        rendered_lines.append(
            f'<text x="{x}" y="{y + index * line_height}" font-family="PingFang SC, Source Han Sans SC, Arial, sans-serif" '
            f'font-size="{font_size}" font-weight="{weight}" fill="{fill}">{escape(line)}</text>'
        )
    return "".join(rendered_lines)


def _canonical_date(text: str) -> str:
    return re.sub(r"(\d{1,2})\.(\d{1,2})", r"\1/\2", text)


def _canonical_date_range(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        return f"{int(match.group(1))}月{int(match.group(2))}日"

    return re.sub(r"(\d{1,2})[./](\d{1,2})", repl, text)


def _week_period_from_title(title: str, fallback: str) -> str:
    match = re.search(r"\bWEEK\s*(\d{1,2})\b", title.upper())
    if not match:
        return fallback
    year = datetime.now().year
    return f"WEEK {int(match.group(1)):02d} · {year}"


def _display_title_and_period(payload: dict[str, Any]) -> tuple[str, str]:
    raw_title = _text(payload.get("title"), "MY WEEK")
    raw_period = _text(payload.get("period"), "TRAINING SUMMARY")
    period_from_title = _week_period_from_title(raw_title, raw_period)

    if re.fullmatch(r"\s*WEEK\s*\d{1,2}\s*", raw_title, flags=re.IGNORECASE):
        return "MY WEEK", _fit_text(period_from_title.upper(), 28)
    if raw_period.strip().upper() in {"TRAINING SUMMARY", "训练总结"}:
        inferred_period = _week_period_from_title(raw_title, raw_period)
        if inferred_period != raw_period:
            return _fit_text(raw_title.upper(), 18), _fit_text(inferred_period.upper(), 28)
    return _fit_text(raw_title.upper(), 18), _fit_text(raw_period.upper(), 28)


def _unit_from_label(metric: Metric) -> str:
    label_icon = f"{metric.label} {metric.icon}".lower()
    if "strength" in label_icon or "力量" in label_icon:
        return "分钟"
    if any(token in label_icon for token in ("run", "bike", "swim", "跑", "骑", "游")):
        return "km"
    return ""


def _duration_from_text(text: str) -> str:
    hour_match = re.search(r"(\d+)\s*h\s*(\d+)\s*min", text, flags=re.IGNORECASE)
    if hour_match:
        return f"{int(hour_match.group(1))}h{int(hour_match.group(2)):02d}m"
    minute_match = re.search(r"(\d+)\s*min", text, flags=re.IGNORECASE)
    if minute_match:
        return f"{int(minute_match.group(1))} 分钟"
    return ""


def _session_count(text: str) -> str:
    match = re.search(r"(\d+(?:\.\d+)?)\s*次", text)
    if not match:
        return ""
    number = match.group(1)
    if number.endswith(".0"):
        number = number[:-2]
    return f"{number}次"


def _normalize_weekly_total(metric: Metric) -> Metric:
    if "次" in metric.value:
        return metric

    sessions = _session_count(metric.subvalue)
    if not sessions:
        return metric

    unit = _unit_from_label(metric)
    subvalue = metric.subvalue
    if unit == "分钟":
        subvalue = _duration_from_text(metric.subvalue) or f"{metric.value} 分钟"
    elif unit == "km":
        prefix = "" if ("骑" in metric.label or "bike" in metric.icon.lower()) else "约 "
        value = metric.value if re.search(r"[a-zA-Z公里]", metric.value) else f"{metric.value} km"
        subvalue = f"{prefix}{value}"

    return Metric(metric.label, sessions, subvalue, metric.icon, metric.color)


def _normalize_weekly_totals(metrics: list[Metric]) -> list[Metric]:
    return [_normalize_weekly_total(metric) for metric in metrics]


def _normalize_key_metric(metric: Metric) -> Metric:
    value = metric.value
    subvalue = metric.subvalue.strip()
    normalized_units = {
        "次": "次",
        "km": "km",
        "公里": "km",
        "米": "m",
        "m": "m",
        "bpm": "bpm",
    }
    if subvalue.lower() in normalized_units and not re.search(r"[a-zA-Z\u4e00-\u9fff]", value):
        value = f"{value} {normalized_units[subvalue.lower()]}"
    value = re.sub(r"(\d+)h\s*(\d+)min", lambda m: f"{int(m.group(1))}h {int(m.group(2))}m", value, flags=re.IGNORECASE)
    return Metric(metric.label, value, metric.subvalue, metric.icon, metric.color)


def _normalize_key_metrics(metrics: list[Metric]) -> list[Metric]:
    return [_normalize_key_metric(metric) for metric in metrics]


def _hardest_score(day: TimelineDay) -> int:
    joined = f"{day.title} {' '.join(day.details)} {day.icon}".lower()
    score = len(day.details)
    if "tri" in joined or "铁三" in joined:
        score += 100
    if "模拟" in joined:
        score += 40
    if "骑" in joined and "游" in joined:
        score += 30
    if "峰值200" in joined or "hr峰值200" in joined or "等效跑量" in joined:
        score += 18
    return score


def _demoted_tone(day: TimelineDay, keep_hardest: bool) -> str:
    if keep_hardest:
        return "hardest"
    joined = f"{day.title} {' '.join(day.details)}".lower()
    if "休息" in joined:
        return "recovery"
    if "恢复跑" in joined or "开放水域" in joined:
        return "normal"
    if "峰值200" in joined or "hr峰值200" in joined or "等效跑量" in joined or "高温" in joined or "30°" in joined:
        return "warning"
    if "节奏" in joined or "高强度" in joined:
        return "warning"
    return "normal"


def _normalize_timeline_tones(days: list[TimelineDay]) -> list[TimelineDay]:
    hardest_indices = [index for index, day in enumerate(days) if day.tone == "hardest"]
    if len(hardest_indices) <= 1:
        return days

    keep_index = max(hardest_indices, key=lambda index: _hardest_score(days[index]))
    normalized: list[TimelineDay] = []
    for index, day in enumerate(days):
        tone = _demoted_tone(day, keep_hardest=index == keep_index) if index in hardest_indices else day.tone
        normalized.append(
            TimelineDay(
                day=day.day,
                date=day.date,
                title=day.title,
                details=day.details,
                tone=tone,
                icon=day.icon,
            )
        )
    return normalized


def _canonical_quote(text: str) -> str:
    if re.search(r"[\u4e00-\u9fff]", text):
        return "SMALL CONSISTENT STEPS, BIG CHANGES."
    return text


def _canonical_headline(text: str) -> str:
    if re.search(r"[\u4e00-\u9fff]", text):
        return "Train Hard, Recover Smart."
    return text


def _metric_list(payload: dict[str, Any], keys: tuple[str, ...], limit: int) -> list[Metric]:
    raw_metrics = None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            raw_metrics = value
            break

    metrics: list[Metric] = []
    if isinstance(raw_metrics, list):
        for item in raw_metrics:
            if not isinstance(item, dict):
                continue
            label = _text(item.get("label")).upper()
            value = _text(item.get("value")).upper()
            subvalue = _text(item.get("subvalue"))
            icon = _text(item.get("icon"))
            color = _text(item.get("color"), "#c8ff47")
            if label and value:
                metrics.append(
                    Metric(
                        label=label,
                        value=value,
                        subvalue=subvalue,
                        icon=icon,
                        color=color,
                    )
                )
    return metrics[:limit]


def _timeline_days(payload: dict[str, Any]) -> tuple[TimelineDay, ...]:
    raw_timeline = payload.get("timeline")
    days: list[TimelineDay] = []
    if isinstance(raw_timeline, list):
        for item in raw_timeline:
            if not isinstance(item, dict):
                continue
            raw_details = item.get("details")
            details = []
            if isinstance(raw_details, list):
                details = [_text(detail) for detail in raw_details if _text(detail)]
            day = TimelineDay(
                day=_fit_text(_text(item.get("day"), "DAY").upper(), 4),
                date=_fit_text(_canonical_date(_text(item.get("date"), "")), 7),
                title=_fit_by_width(_text(item.get("title"), "TRAINING").upper(), 30),
                details=tuple(details[:5]),
                tone=_text(item.get("tone"), "normal").lower(),
                icon=_text(item.get("icon")),
            )
            days.append(day)

    if days:
        return tuple(_normalize_timeline_tones(days[:7]))

    return tuple(
        TimelineDay(day=day, date="", title="NO DATA", details=(), tone="normal", icon="")
        for day in ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    )


def normalize_poster_data(payload: dict[str, Any]) -> PosterData:
    weekly_totals = _metric_list(payload, ("weekly_totals", "totals", "metrics"), 4)
    key_metrics = _metric_list(payload, ("key_metrics", "metrics"), 5)
    title, period = _display_title_and_period(payload)

    if not weekly_totals:
        weekly_totals = [
            Metric("跑步", _text(payload.get("run"), "--"), icon="run", color="#8cff3f"),
            Metric("骑行", _text(payload.get("bike"), "--"), icon="bike", color="#ffd21f"),
            Metric("游泳", _text(payload.get("swim"), "--"), icon="swim", color="#35c8ff"),
            Metric("力量", _text(payload.get("strength"), "--"), icon="strength", color="#a477ff"),
        ]

    if not key_metrics:
        key_metrics = [
            Metric("SESSIONS", _text(payload.get("sessions"), "0"), icon="star", color="#c8ff47"),
            Metric("TOTAL TIME", _text(payload.get("total_time"), "0M"), icon="clock", color="#9cff3f"),
        ]

    weekly_totals = _normalize_weekly_totals(weekly_totals)
    key_metrics = _normalize_key_metrics(key_metrics)

    raw_recommendations = payload.get("recommendations")
    recommendations: list[str] = []
    if isinstance(raw_recommendations, list):
        recommendations = [_text(item) for item in raw_recommendations if _text(item)]

    return PosterData(
        title=title,
        period=period,
        main_title=_fit_text(_text(payload.get("main_title"), "训练总结"), 10),
        subtitle=_fit_text(_text(payload.get("subtitle"), "Training Summary"), 24),
        date_range=_fit_text(_canonical_date_range(_text(payload.get("date_range"), "")), 32),
        headline=_fit_text(_canonical_headline(_text(payload.get("headline"), "Train Hard, Recover Smart.")), 34),
        weekly_totals=tuple(weekly_totals[:4]),
        key_metrics=tuple(key_metrics[:5]),
        highlight=_fit_text(
            _text(payload.get("highlight"), "CONSISTENCY WINS").upper(),
            32,
        ),
        insights=_fit_text(
            _text(
                payload.get("insights"),
                _text(payload.get("recommendations"), "Keep the rhythm. Recover well."),
            ),
            82,
        ),
        recommendations=tuple(recommendations[:4]),
        quote=_fit_text(
            _canonical_quote(_text(payload.get("quote"), "SMALL CONSISTENT STEPS, BIG CHANGES.")),
            58,
        ),
        timeline=_timeline_days(payload),
    )


def _metric_lines(metrics: tuple[Metric, ...], x: int, y: int, row_gap: int = 50) -> str:
    lines = []
    for index, metric in enumerate(metrics):
        row_y = y + index * row_gap
        lines.append(
            f"""
    <text x="{x}" y="{row_y}" class="small-label" font-family="Helvetica Neue, Arial, sans-serif" font-size="20" font-weight="800" fill="rgba(255,255,255,0.58)" letter-spacing="2">{escape(_fit_text(metric.label, 14))}</text>
    <text x="{x + 232}" y="{row_y}" class="small-value" text-anchor="end" font-family="Helvetica Neue, Arial, sans-serif" font-size="32" font-weight="900" fill="#ffffff">{escape(_fit_text(metric.value, 12))}</text>"""
        )
    return "".join(lines)


def _timeline_accent(tone: str) -> str:
    if tone == "hardest":
        return "#f6c85f"
    if tone == "recovery":
        return "#4db7ff"
    if tone == "warning":
        return "#ff6b6b"
    return "#c8ff47"


def _icon_symbol(icon: str) -> str:
    return {
        "run": "R",
        "bike": "B",
        "swim": "S",
        "strength": "F",
        "rest": "Z",
        "tri": "T",
        "clock": "◷",
        "fire": "K",
        "mountain": "▲",
        "heart": "♥",
        "star": "★",
    }.get(icon, "●")


@cache
def _icon_data_uri(filename: str) -> str:
    path = ICON_ASSET_DIR / filename
    if not path.exists():
        return ""
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _asset_class(filename: str) -> str:
    return f"asset-icon-{Path(filename).stem.replace('-', '_')}"


def _png_icon_svg(
    filename: str,
    x: int,
    y: int,
    width: int,
    height: int,
    classes: str,
    opacity: float = 1.0,
) -> str:
    data_uri = _icon_data_uri(filename)
    if not data_uri:
        return ""
    return (
        f'<image class="{classes} embedded-png-icon-assets {_asset_class(filename)}" '
        f'x="{x}" y="{y}" width="{width}" height="{height}" href="{data_uri}" '
        f'preserveAspectRatio="xMidYMid meet" opacity="{opacity:.2f}"/>'
    )


def _activity_icon_svg(icon: str, x: int, y: int, size: int, color: str) -> str:
    asset = ACTIVITY_ICON_ASSETS.get(icon)
    if icon == "tri":
        small = max(16, int(size * 0.42))
        return f"""
  <g class="activity-icon-tri activity-icon-tri-reference embedded-png-icon-assets">
    {_png_icon_svg("running_green.png", x, y, small, small, "tri-icon-part")}
    {_png_icon_svg("cycling_yellow.png", x + int(size * 0.32), y, small, small, "tri-icon-part")}
    {_png_icon_svg("swimming_blue.png", x + int(size * 0.64), y, small, small, "tri-icon-part")}
  </g>"""
    if asset:
        return _png_icon_svg(
            asset,
            x,
            y,
            size,
            size,
            f"activity-icon-{escape(icon)} activity-icon-{escape(icon)}-reference",
        )

    scale = size / 64
    common = (
        f'<g class="activity-icon-{escape(icon)} activity-icon-{escape(icon)}-reference" '
        f'transform="translate({x} {y}) scale({scale:.4f})" '
        f'stroke="{color}" fill="none" stroke-linecap="round" stroke-linejoin="round">'
    )
    if icon == "bike":
        body = """
    <circle cx="16" cy="47" r="11" stroke-width="4"/>
    <circle cx="48" cy="47" r="11" stroke-width="4"/>
    <path d="M16 47 L27 31 L38 47 L27 47 L36 31 L48 47" stroke-width="4"/>
    <circle cx="37" cy="15" r="5" fill="currentColor" stroke="none"/>
    <path d="M36 23 L28 31 L41 31 L48 23" stroke-width="5"/>
    <path d="M41 31 L34 42" stroke-width="5"/>
"""
    elif icon == "swim":
        body = """
    <path d="M7 46 C15 39 22 53 31 46 S47 39 57 46" stroke-width="5"/>
    <path d="M7 56 C15 49 22 63 31 56 S47 49 57 56" stroke-width="5"/>
    <path d="M14 34 C24 29 34 28 45 34" stroke-width="5"/>
    <path d="M23 30 L35 18 L48 33" stroke-width="6"/>
    <circle cx="45" cy="24" r="5" fill="currentColor" stroke="none"/>
"""
    elif icon == "strength":
        body = """
    <path d="M10 32 H54" stroke-width="7"/>
    <path d="M17 20 V44 M23 18 V46 M41 18 V46 M47 20 V44" stroke-width="7"/>
    <path d="M29 32 H35" stroke-width="9"/>
"""
    elif icon == "rest":
        body = """
    <circle cx="32" cy="13" r="7" fill="currentColor" stroke="none"/>
    <path d="M32 23 C25 29 23 38 27 45" stroke-width="5"/>
    <path d="M32 23 C39 29 41 38 37 45" stroke-width="5"/>
    <path d="M28 43 C20 48 14 52 10 58" stroke-width="5"/>
    <path d="M36 43 C44 48 50 52 54 58" stroke-width="5"/>
    <path d="M21 58 H43" stroke-width="5"/>
"""
    elif icon == "tri":
        body = """
    <g transform="translate(0 0) scale(.62)">
      <circle cx="25" cy="10" r="5" fill="currentColor" stroke="none"/>
      <path d="M25 17 L17 29 L29 30 L38 42" stroke-width="5"/>
      <path d="M20 32 L12 44" stroke-width="5"/>
    </g>
    <g transform="translate(25 18) scale(.52)">
      <circle cx="14" cy="44" r="9" stroke-width="5"/>
      <circle cx="50" cy="44" r="9" stroke-width="5"/>
      <path d="M14 44 L28 28 L39 44 L28 44 L36 28 L50 44" stroke-width="5"/>
    </g>
    <g transform="translate(2 45) scale(.55)">
      <path d="M3 21 C13 13 21 29 32 21 S50 13 61 21" stroke-width="5"/>
      <path d="M3 34 C13 26 21 42 32 34 S50 26 61 34" stroke-width="5"/>
    </g>
"""
    else:
        body = """
    <circle cx="36" cy="10" r="6" fill="currentColor" stroke="none"/>
    <path d="M33 19 L25 33 L38 34 L50 45" stroke-width="6"/>
    <path d="M28 30 L16 27" stroke-width="5"/>
    <path d="M38 34 L31 54" stroke-width="6"/>
    <path d="M25 33 L14 51" stroke-width="6"/>
"""
    body = body.replace("currentColor", color)
    return f"""{common}
    <style>.activity-icon-{escape(icon)} {{ color: {color}; }}</style>
{body}
  </g>"""


def _section_card(x: int, y: int, w: int, h: int, title: str, icon: str = "") -> str:
    section_icon = f'<text x="{x + 24}" y="{y + 42}" font-family="Apple Color Emoji, Helvetica Neue, Arial, sans-serif" font-size="26">{escape(icon)}</text>' if icon else ""
    title_x = x + 58 if icon else x + 24
    return f"""
  <g filter="url(#cardShadow)">
    <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="rgba(5,24,42,0.74)" stroke="rgba(108,191,238,0.58)" stroke-width="1.2"/>
    <rect x="{x}" y="{y}" width="{w}" height="54" rx="14" fill="rgba(255,255,255,0.055)"/>
    {section_icon}
    <text x="{title_x}" y="{y + 38}" font-family="PingFang SC, Helvetica Neue, Arial, sans-serif" font-size="28" font-weight="900" fill="#ffffff">{escape(title)}</text>
  </g>"""


def _summary_grid(metrics: tuple[Metric, ...]) -> str:
    cells = [(48, 366), (288, 366), (48, 500), (288, 500)]
    parts = []
    for index, metric in enumerate(metrics[:4]):
        x, y = cells[index]
        color = metric.color
        parts.append(
            f"""
  <g>
    {_activity_icon_svg(metric.icon or metric.label.lower(), x + 9, y + 15, 64, color)}
    <text x="{x + 104}" y="{y + 31}" font-family="PingFang SC, Helvetica Neue, Arial, sans-serif" font-size="22" font-weight="900" fill="{color}">{escape(metric.label)}</text>
    <text x="{x + 104}" y="{y + 76}" font-family="PingFang SC, Helvetica Neue, Arial, sans-serif" font-size="41" font-weight="900" fill="#ffffff">{escape(metric.value)}</text>
    <text x="{x + 104}" y="{y + 109}" font-family="PingFang SC, Helvetica Neue, Arial, sans-serif" font-size="24" font-weight="800" fill="{color}">{escape(metric.subvalue)}</text>
  </g>"""
        )
    return "".join(parts)


def _key_metric_grid(metrics: tuple[Metric, ...]) -> str:
    x_positions = [76, 195, 315, 438]
    parts = []
    for index, metric in enumerate(metrics[:4]):
        x = x_positions[index]
        color = metric.color
        divider = (
            f'<path d="M{x - 18} 718 V812" stroke="rgba(108,191,238,0.42)" stroke-width="1"/>'
            if index > 0
            else ""
        )
        parts.append(
            f"""
  <g>
    {divider}
    <text x="{x}" y="740" font-family="Helvetica Neue, Arial, sans-serif" font-size="34" font-weight="900" fill="{color}" text-anchor="middle">{escape(_icon_symbol(metric.icon))}</text>
    <text x="{x}" y="776" font-family="PingFang SC, Helvetica Neue, Arial, sans-serif" font-size="18" font-weight="700" fill="#ffffff" text-anchor="middle">{escape(metric.label)}</text>
    <text x="{x}" y="814" font-family="PingFang SC, Helvetica Neue, Arial, sans-serif" font-size="23" font-weight="900" fill="{color}" text-anchor="middle">{escape(metric.value)}</text>
  </g>"""
        )
    return "".join(parts)


def _recommendation_lines(insights: str, recommendations: tuple[str, ...]) -> str:
    lines = [_fit_by_width(insights, 54)]
    lines.extend(_fit_by_width(item, 54) for item in recommendations[:3])
    colors = ["#8cff3f", "#ffd21f", "#8cff3f", "#35c8ff"]
    icons = ["↗", "⚠", "▣", "☾"]
    parts = []
    for index, line in enumerate(lines[:4]):
        y = 1118 + index * 36
        parts.append(
            f"""
  <g>
    <circle cx="63" cy="{y - 9}" r="16" fill="rgba(65,226,188,0.15)"/>
    <text x="63" y="{y - 1}" font-family="Helvetica Neue, Arial, sans-serif" font-size="20" font-weight="900" fill="{colors[index]}" text-anchor="middle">{icons[index]}</text>
    <text x="94" y="{y}" font-family="PingFang SC, Helvetica Neue, Arial, sans-serif" font-size="16" font-weight="650" fill="#ffffff">{escape(line)}</text>
  </g>"""
        )
    return "".join(parts)


def _timeline_cards(days: tuple[TimelineDay, ...]) -> str:
    cards: list[str] = []
    start_x = 590
    start_y = 82
    card_w = 430
    base_h = 128
    gap = 10
    line_x = 562
    for index, day in enumerate(days):
        h = 164 if day.tone == "hardest" else base_h
        y = start_y + index * (base_h + gap)
        if index > 5:
            y += 46
        accent = _timeline_accent(day.tone)
        fill = "rgba(33,25,24,0.72)" if day.tone == "hardest" else "rgba(5,24,42,0.72)"
        badge_fill = "rgba(246,200,95,0.78)" if day.tone == "hardest" else "rgba(79,226,188,0.90)"
        date_fill = "rgba(86,128,166,0.54)"
        detail_lines = []
        for detail_index, detail in enumerate(day.details):
            if day.tone == "hardest":
                row_y = y + 94 + detail_index * 25
            else:
                row_y = y + 94 + detail_index * 25
            detail_lines.append(
                f'<text x="{start_x + 48}" y="{row_y}" font-family="PingFang SC, Helvetica Neue, Arial, sans-serif" font-size="15.5" font-weight="650" fill="#ffffff">{escape(_fit_by_width(detail, 43))}</text>'
            )
        cards.append(
            f"""
  <g filter="url(#cardShadow)">
    <circle cx="{line_x}" cy="{y + 13}" r="8" fill="{accent}" stroke="rgba(255,255,255,0.55)" stroke-width="2"/>
    <rect x="{start_x}" y="{y}" width="{card_w}" height="{h}" rx="13" fill="{fill}" stroke="{accent}" stroke-opacity="0.68" stroke-width="1"/>
    <rect x="{start_x + 14}" y="{y + 12}" width="58" height="30" rx="8" fill="{badge_fill}"/>
    <text x="{start_x + 43}" y="{y + 34}" font-family="PingFang SC, Helvetica Neue, Arial, sans-serif" font-size="18" font-weight="900" fill="#061426" text-anchor="middle">{escape(day.day)}</text>
    <rect x="{start_x + 76}" y="{y + 12}" width="68" height="30" rx="8" fill="{date_fill}"/>
    <text x="{start_x + 110}" y="{y + 34}" font-family="Helvetica Neue, Arial, sans-serif" font-size="18" font-weight="900" fill="#ffffff" text-anchor="middle">{escape(day.date)}</text>
    {_activity_icon_svg(day.icon or "run", start_x + 14, y + 48, 43, accent)}
    <text x="{start_x + 64}" y="{y + 70}" font-family="PingFang SC, Helvetica Neue, Arial, sans-serif" font-size="22" font-weight="900" fill="#ffffff">{escape(day.title)}</text>
    <path d="M{start_x + 20} {y + 88} H{start_x + card_w - 20}" stroke="rgba(255,255,255,0.15)" stroke-width="1"/>
    {''.join(detail_lines)}
  </g>"""
        )
    return f"""
  <path d="M{line_x} 70 V1248" stroke="rgba(79,226,188,0.62)" stroke-width="2"/>
  {''.join(cards)}"""


def _outline_icon_svg(icon: str, x: int, y: int, size: int, color: str) -> str:
    asset = OUTLINE_ICON_ASSETS.get(icon)
    if asset:
        return _png_icon_svg(
            asset,
            x,
            y,
            size,
            size,
            f"material-outline-icon material-outline-icon-{escape(icon)}",
        )

    scale = size / 64
    common = (
        f'<g class="material-outline-icon material-outline-icon-{escape(icon)}" '
        f'transform="translate({x} {y}) scale({scale:.4f})" '
        f'stroke="{color}" fill="none" stroke-linecap="round" stroke-linejoin="round">'
    )
    if icon == "clock":
        body = """
    <circle cx="32" cy="32" r="24" stroke-width="5"/>
    <path d="M32 18 V34 L44 41" stroke-width="5"/>
"""
    elif icon == "fire":
        body = """
    <path d="M34 57 C48 50 54 39 48 27 C45 20 40 16 39 8 C30 14 28 23 31 30 C25 27 22 22 22 17 C12 27 10 39 16 49 C20 56 27 60 34 57Z" stroke-width="5"/>
    <path d="M33 54 C40 50 42 43 38 36 C35 39 31 41 28 46 C26 50 28 54 33 54Z" stroke-width="4"/>
"""
    elif icon == "mountain":
        body = """
    <path d="M7 53 L25 20 L34 35 L42 25 L57 53 Z" stroke-width="5"/>
    <path d="M25 20 L31 35 L34 35" stroke-width="4"/>
"""
    elif icon == "heart":
        body = """
    <path d="M32 55 C20 45 10 36 10 24 C10 15 16 10 24 10 C29 10 32 13 32 13 C32 13 35 10 40 10 C48 10 54 15 54 24 C54 36 44 45 32 55Z" stroke-width="5"/>
    <path d="M16 32 H25 L29 25 L36 41 L41 32 H49" stroke-width="4"/>
"""
    elif icon == "sleep":
        body = """
    <path d="M43 10 C34 13 28 21 28 32 C28 43 36 51 47 54 C43 57 38 59 32 59 C17 59 5 47 5 32 C5 17 17 5 32 5 C36 5 40 7 43 10Z" stroke-width="5"/>
    <path d="M45 19 H56 L45 31 H57" stroke-width="4"/>
"""
    elif icon == "battery":
        body = """
    <rect x="8" y="18" width="44" height="28" rx="6" stroke-width="5"/>
    <path d="M55 27 V37" stroke-width="5"/>
    <path d="M18 32 H36" stroke-width="5"/>
"""
    elif icon == "warning":
        body = """
    <path d="M32 8 L59 55 H5 Z" stroke-width="5"/>
    <path d="M32 24 V38" stroke-width="5"/>
    <path d="M32 48 H32.5" stroke-width="7"/>
"""
    elif icon == "trend":
        body = """
    <path d="M8 48 L22 34 L34 40 L54 18" stroke-width="5"/>
    <path d="M42 18 H54 V30" stroke-width="5"/>
"""
    elif icon == "bars":
        body = """
    <path d="M10 52 H56" stroke-width="5"/>
    <rect x="14" y="34" width="8" height="18" rx="2" stroke-width="4"/>
    <rect x="29" y="24" width="8" height="28" rx="2" stroke-width="4"/>
    <rect x="44" y="14" width="8" height="38" rx="2" stroke-width="4"/>
"""
    elif icon == "trophy":
        body = """
    <path d="M21 10 H43 V25 C43 35 38 40 32 40 C26 40 21 35 21 25Z" stroke-width="5"/>
    <path d="M21 16 H11 C11 27 15 33 23 34" stroke-width="4"/>
    <path d="M43 16 H53 C53 27 49 33 41 34" stroke-width="4"/>
    <path d="M32 40 V51" stroke-width="5"/>
    <path d="M22 55 H42" stroke-width="5"/>
"""
    elif icon == "check":
        body = """
    <circle cx="32" cy="32" r="25" stroke-width="5"/>
    <path d="M20 33 L29 42 L46 22" stroke-width="5"/>
"""
    elif icon == "spark":
        body = """
    <path d="M32 6 L37 25 L56 32 L37 39 L32 58 L27 39 L8 32 L27 25 Z" stroke-width="5"/>
"""
    else:
        body = """
    <circle cx="32" cy="32" r="24" stroke-width="5"/>
    <path d="M22 32 H42" stroke-width="5"/>
    <path d="M32 22 V42" stroke-width="5"/>
"""
    return f"""{common}
{body}
  </g>"""


def _glass_rect(x: int, y: int, w: int, h: int, rx: int = 28, accent: str = "#36e3d4", opacity: float = 0.58) -> str:
    return f"""
  <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="url(#cardGlass)" stroke="{accent}" stroke-opacity="{opacity}" stroke-width="1.6"/>
  <rect x="{x + 1}" y="{y + 1}" width="{w - 2}" height="{max(0, h // 3)}" rx="{rx}" fill="#ffffff" fill-opacity="0.045"/>"""


def _summary_dashboard_grid(metrics: tuple[Metric, ...], x: int, y: int, w: int, h: int) -> str:
    cell_w = (w - 72) // 2
    cell_h = (h - 144) // 2
    positions = [
        (x + 36, y + 104),
        (x + 36 + cell_w + 24, y + 104),
        (x + 36, y + 104 + cell_h + 24),
        (x + 36 + cell_w + 24, y + 104 + cell_h + 24),
    ]
    parts = [
        f'<path d="M{x + w // 2} {y + 112} V{y + h - 40}" stroke="#6cbfee" stroke-opacity="0.24" stroke-width="1.5"/>',
        f'<path d="M{x + 36} {y + 104 + cell_h + 12} H{x + w - 36}" stroke="#6cbfee" stroke-opacity="0.20" stroke-width="1.5"/>',
    ]
    for index, metric in enumerate(metrics[:4]):
        cx, cy = positions[index]
        color = metric.color
        parts.append(
            f"""
  <g class="large-number-priority">
    {_activity_icon_svg(metric.icon or metric.label.lower(), cx, cy + 18, 106, color)}
    <text x="{cx + 128}" y="{cy + 52}" font-family="PingFang SC, Helvetica Neue, Arial, sans-serif" font-size="34" font-weight="900" fill="{color}">{escape(_fit_by_width(metric.label, 10))}</text>
    <text x="{cx + 128}" y="{cy + 128}" font-family="PingFang SC, Helvetica Neue, Arial, sans-serif" font-size="72" font-weight="950" fill="#ffffff">{escape(_fit_by_width(metric.value, 12))}</text>
    <text x="{cx + 128}" y="{cy + 176}" font-family="PingFang SC, Helvetica Neue, Arial, sans-serif" font-size="36" font-weight="850" fill="{color}">{escape(_fit_by_width(metric.subvalue, 14))}</text>
  </g>"""
        )
    return "".join(parts)


def _key_metrics_panel(metrics: tuple[Metric, ...], x: int, y: int, w: int) -> str:
    cell_w = (w - 86) // 2
    cell_h = 118
    parts = []
    for index, metric in enumerate(metrics[:4]):
        col = index % 2
        row = index // 2
        cx = x + 36 + col * (cell_w + 14)
        cy = y + 94 + row * (cell_h + 18)
        color = metric.color
        parts.append(
            f"""
  <g>
    <rect x="{cx}" y="{cy}" width="{cell_w}" height="{cell_h}" rx="22" fill="#071a2a" fill-opacity="0.58" stroke="#6cbfee" stroke-opacity="0.18"/>
    {_outline_icon_svg(metric.icon or "spark", cx + 20, cy + 28, 54, color)}
    <text x="{cx + 90}" y="{cy + 42}" font-family="PingFang SC, Helvetica Neue, Arial, sans-serif" font-size="23" font-weight="800" fill="#a9bfd0">{escape(_fit_by_width(metric.label, 16))}</text>
    <text x="{cx + 90}" y="{cy + 92}" font-family="Helvetica Neue, PingFang SC, Arial, sans-serif" font-size="42" font-weight="950" fill="{color}">{escape(_fit_by_width(metric.value, 16))}</text>
  </g>"""
        )
    return "".join(parts)


def _key_metrics_strip(metrics: tuple[Metric, ...], x: int, y: int, w: int) -> str:
    col_w = (w - 72) // 4
    parts = ['<g class="compact-key-metric-strip">']
    for index, metric in enumerate(metrics[:4]):
        cx = x + 36 + col_w * index
        center = cx + col_w // 2
        color = metric.color
        if index > 0:
            parts.append(
                f'<path d="M{cx} {y + 112} V{y + 300}" stroke="#6cbfee" stroke-opacity="0.34" stroke-width="1.4"/>'
            )
        parts.append(
            f"""
  <g>
    {_outline_icon_svg(metric.icon or "spark", center - 32, y + 120, 64, color)}
    <text x="{center}" y="{y + 218}" font-family="PingFang SC, Helvetica Neue, Arial, sans-serif" font-size="25" font-weight="760" fill="#ffffff" text-anchor="middle">{escape(_fit_by_width(metric.label, 9))}</text>
    {_key_metric_value_svg(metric.value, center, y + 280, color)}
  </g>"""
        )
    parts.append("</g>")
    return "".join(parts)


def _key_metric_value_svg(value: str, x: int, y: int, color: str) -> str:
    parts = value.rsplit(" ", 1)
    if len(parts) == 2 and any(char.isalpha() for char in parts[1]) and len(parts[1]) <= 5:
        number, unit = parts
        return f"""
    <text x="{x - 6}" y="{y}" font-family="Helvetica Neue Condensed Black, Helvetica Neue, PingFang SC, Arial, sans-serif" font-size="39" font-weight="950" fill="{color}" text-anchor="end">{escape(_fit_by_width(number, 8))}</text>
    <text x="{x - 2}" y="{y}" font-family="Helvetica Neue Condensed Black, Helvetica Neue, PingFang SC, Arial, sans-serif" font-size="24" font-weight="900" fill="#ffffff" fill-opacity="0.88">{escape(unit)}</text>"""
    return f'<text x="{x}" y="{y}" font-family="Helvetica Neue Condensed Black, Helvetica Neue, PingFang SC, Arial, sans-serif" font-size="38" font-weight="950" fill="{color}" text-anchor="middle">{escape(_fit_by_width(value, 10))}</text>'


def _section_heading(title: str, icon: str, x: int, y: int, color: str = "#ffffff") -> str:
    return f"""
  <g class="section-title-outline-icon">
    {_outline_icon_svg(icon, x, y - 42, 46, color)}
    <text x="{x + 60}" y="{y}" font-family="PingFang SC, Helvetica Neue, Arial, sans-serif" font-size="44" font-weight="950" fill="#ffffff">{escape(title)}</text>
  </g>"""


def _highlight_panel(data: PosterData, x: int, y: int) -> str:
    return f"""
  <g>
    {_outline_icon_svg("trophy", x + 36, y + 88, 72, "#f6c85f")}
    <text x="{x + 126}" y="{y + 126}" font-family="PingFang SC, Helvetica Neue, Arial, sans-serif" font-size="52" font-weight="950" fill="#9bff3f">{escape(_fit_by_width(data.highlight, 20))}</text>
    <text x="{x + 36}" y="{y + 198}" font-family="PingFang SC, Helvetica Neue, Arial, sans-serif" font-size="27" font-weight="720" fill="#ffffff" fill-opacity="0.92">{escape(_fit_by_width(data.insights, 44))}</text>
    {_activity_icon_svg("run", x + 472, y + 100, 58, "#8cff3f")}
    {_activity_icon_svg("bike", x + 558, y + 100, 58, "#ffd21f")}
    {_activity_icon_svg("swim", x + 644, y + 100, 58, "#35c8ff")}
    <path d="M{x + 464} {y + 180} C{x + 536} {y + 124}, {x + 604} {y + 142}, {x + 682} {y + 108}" fill="none" stroke="#36e3d4" stroke-opacity="0.32" stroke-width="4"/>
  </g>"""


def _trend_panel(insights: str, recommendations: tuple[str, ...], x: int, y: int) -> str:
    lines = [_fit_by_width(insights, 42)]
    lines.extend(_fit_by_width(item, 42) for item in recommendations[:4])
    icons = ["trend", "check", "spark", "sleep", "warning"]
    colors = ["#8cff3f", "#36e3d4", "#f6c85f", "#4db7ff", "#ff6b6b"]
    parts = []
    for index, line in enumerate(lines[:5]):
        row_y = y + 100 + index * 78
        parts.append(
            f"""
  <g>
    <circle cx="{x + 64}" cy="{row_y - 16}" r="26" fill="{colors[index]}" fill-opacity="0.16"/>
    {_outline_icon_svg(icons[index], x + 42, row_y - 38, 44, colors[index])}
    <text x="{x + 112}" y="{row_y}" font-family="PingFang SC, Helvetica Neue, Arial, sans-serif" font-size="27" font-weight="720" fill="#ffffff">{escape(line)}</text>
  </g>"""
        )
    return "".join(parts)


def _timeline_cards_v2(days: tuple[TimelineDay, ...], x: int, y: int, w: int) -> str:
    cards: list[str] = []
    line_x = x - 52
    current_y = y
    for _index, day in enumerate(days):
        is_hardest = day.tone == "hardest"
        h = 312 if is_hardest else 224
        accent = _timeline_accent(day.tone)
        card_fill = "#161615" if is_hardest else "#061d31"
        badge_fill = "#f6c85f" if is_hardest else ("#4db7ff" if day.tone == "recovery" else "#4fe2bc")
        date_fill = "#234260"
        if is_hardest:
            details = [
                _hardest_breakdown_row(detail, x + 28, current_y + 138 + detail_index * 56, w - 56, detail_index)
                for detail_index, detail in enumerate(day.details[:3])
            ]
        else:
            details = []
            for detail_index, detail in enumerate(day.details[:3]):
                detail_y = current_y + 158 + detail_index * 38
                details.append(
                    f'<text x="{x + 124}" y="{detail_y}" font-family="PingFang SC, Helvetica Neue, Arial, sans-serif" font-size="25" font-weight="650" fill="#ffffff" fill-opacity="0.92">{escape(_fit_by_width(detail, 46))}</text>'
                )
                details.append(
                    f'<circle cx="{x + 98}" cy="{detail_y - 8}" r="5" fill="{accent}" fill-opacity="0.85"/>'
                )
        hard_badge = ""
        if is_hardest:
            hard_badge = f"""
    <rect x="{x + w - 212}" y="{current_y + 28}" width="166" height="44" rx="16" fill="#f6c85f" fill-opacity="0.18" stroke="#f6c85f" stroke-opacity="0.72"/>
    <text x="{x + w - 129}" y="{current_y + 58}" font-family="Helvetica Neue, Arial, sans-serif" font-size="23" font-weight="950" fill="#f6c85f" text-anchor="middle">HARD DAY</text>"""
        side_icons = (
            f"""
    <g class="timeline-side-activity-icons">
      {_activity_icon_svg("run", x + w - 224, current_y + 98, 48, "#8cff3f")}
      {_activity_icon_svg("bike", x + w - 158, current_y + 98, 48, "#ffd21f")}
      {_activity_icon_svg("swim", x + w - 92, current_y + 98, 48, "#35c8ff")}
    </g>"""
            if is_hardest
            else f"""
    <g class="timeline-side-activity-icons" opacity="0.96">
      {_activity_icon_svg(day.icon or "run", x + w - 160, current_y + 48, 94, accent)}
    </g>"""
        )
        cards.append(
            f"""
  <g class="timeline-day-card" filter="url(#cardShadow)">
    <circle cx="{line_x}" cy="{current_y + 38}" r="16" fill="{accent}" stroke="#ffffff" stroke-opacity="0.55" stroke-width="4"/>
    <rect x="{x}" y="{current_y}" width="{w}" height="{h}" rx="26" fill="{card_fill}" fill-opacity="0.74" stroke="{accent}" stroke-opacity="0.72" stroke-width="{2.4 if is_hardest else 1.6}"/>
    <rect x="{x}" y="{current_y}" width="{w}" height="76" rx="26" fill="#ffffff" fill-opacity="{0.055 if not is_hardest else 0.075}"/>
    <rect x="{x + 28}" y="{current_y + 22}" width="112" height="48" rx="15" fill="{badge_fill}"/>
    <text x="{x + 84}" y="{current_y + 56}" font-family="PingFang SC, Helvetica Neue, Arial, sans-serif" font-size="28" font-weight="950" fill="#04121f" text-anchor="middle">{escape(day.day)}</text>
    <rect x="{x + 154}" y="{current_y + 22}" width="128" height="48" rx="15" fill="{date_fill}" fill-opacity="0.86"/>
    <text x="{x + 218}" y="{current_y + 56}" font-family="Helvetica Neue, Arial, sans-serif" font-size="28" font-weight="950" fill="#ffffff" text-anchor="middle">{escape(day.date)}</text>
    {hard_badge}
    {side_icons}
    {_activity_icon_svg(day.icon or "run", x + 30, current_y + 94, 76, accent)}
    <text x="{x + 124}" y="{current_y + 126}" font-family="PingFang SC, Helvetica Neue, Arial, sans-serif" font-size="38" font-weight="950" fill="{accent if is_hardest else '#ffffff'}">{escape(_fit_by_width(day.title, 24))}</text>
    <path d="M{x + 30} {current_y + 142} H{x + w - 30}" stroke="#ffffff" stroke-opacity="0.13" stroke-width="1.4"/>
    {''.join(details)}
  </g>"""
        )
        current_y += h + 24
    return f"""
  <path d="M{line_x} {y - 30} V{current_y - 26}" stroke="#4fe2bc" stroke-opacity="0.52" stroke-width="4"/>
  {''.join(cards)}"""


def _hardest_breakdown_row(detail: str, x: int, y: int, w: int, index: int) -> str:
    sport, color = _detail_sport(detail, index)
    label = {"run": "跑步", "bike": "骑行", "swim": "游泳"}.get(sport, "训练")
    text = detail
    if " " in detail:
        text = detail.split(" ", 1)[1]
    return f"""
    <g class="hardest-day-sport-breakdown">
      <rect x="{x}" y="{y}" width="{w}" height="46" rx="12" fill="#ffffff" fill-opacity="0.055" stroke="{color}" stroke-opacity="0.30"/>
      {_activity_icon_svg(sport, x + 16, y + 3, 40, color)}
      <text x="{x + 78}" y="{y + 31}" font-family="PingFang SC, Helvetica Neue, Arial, sans-serif" font-size="24" font-weight="900" fill="{color}">{escape(label)}</text>
      <path d="M{x + 154} {y + 10} V{y + 36}" stroke="#ffffff" stroke-opacity="0.18"/>
      <text x="{x + 178}" y="{y + 31}" font-family="PingFang SC, Helvetica Neue, Arial, sans-serif" font-size="24" font-weight="650" fill="#ffffff" fill-opacity="0.94">{escape(_fit_by_width(text, 42))}</text>
    </g>"""


def _detail_sport(detail: str, index: int) -> tuple[str, str]:
    if "跑" in detail and "骑" in detail and "游" in detail:
        return "tri", "#ffd21f"
    if "骑" in detail:
        return "bike", "#ffd21f"
    if "游" in detail:
        return "swim", "#35c8ff"
    if "跑" in detail:
        return "run", "#8cff3f"
    fallback = [("run", "#8cff3f"), ("bike", "#ffd21f"), ("swim", "#35c8ff")]
    return fallback[index % len(fallback)]


def _compact_card(x: int, y: int, w: int, h: int, accent: str = "#3F7894", rx: int = 13) -> str:
    return f"""
    <g class="glass-card-inner-glow">
      <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="url(#cardGlass)" stroke="{accent}" stroke-opacity="0.72" stroke-width="1"/>
      <rect x="{x + 1}" y="{y + 1}" width="{w - 2}" height="{h - 2}" rx="{rx - 1}" fill="url(#cardInnerGlow)" opacity="0.58"/>
      <rect x="{x + 8}" y="{y + 8}" width="{w - 16}" height="{h - 16}" rx="{rx - 5}" fill="none" stroke="#35E7C2" stroke-opacity="0.075" stroke-width="1"/>
      <rect x="{x}" y="{y}" width="{w}" height="52" rx="{rx}" fill="url(#cardTop)" opacity="0.72"/>
      <path d="M{x + 14} {y + h - 9} H{x + w - 14}" stroke="#35E7C2" stroke-opacity="0.09" stroke-width="1"/>
    </g>"""


def _compact_heading(title: str, icon: str, x: int, y: int, color: str = "#ffffff") -> str:
    return f"""
    <g class="section-title-outline-icon">
      {_outline_icon_svg(icon, x, y - 26, 31, color)}
      <text x="{x + 38}" y="{y}" font-family="PingFang SC, Source Han Sans SC, Arial, sans-serif" font-size="26" font-weight="950" fill="#F4F7FA">{escape(title)}</text>
    </g>"""


def _compact_summary_grid(metrics: tuple[Metric, ...]) -> str:
    cells = [(48, 366), (288, 366), (48, 500), (288, 500)]
    parts = [
        '<g class="large-number-priority compact-summary-grid summary-cell-contained">',
        '<path d="M265 360 V602" stroke="#3F7894" stroke-opacity="0.72" stroke-width="1"/>',
        '<path d="M48 482 H482" stroke="#3F7894" stroke-opacity="0.52" stroke-width="1"/>',
    ]
    for index, metric in enumerate(metrics[:4]):
        x, y = cells[index]
        color = metric.color
        parts.append(
            f"""
      <g>
        {_activity_icon_svg(metric.icon or metric.label.lower(), x + 12, y + 24, 58, color)}
        <text x="{x + 96}" y="{y + 35}" font-family="PingFang SC, Source Han Sans SC, Arial, sans-serif" font-size="21" font-weight="900" fill="{color}">{escape(_fit_by_width(metric.label, 8))}</text>
        <text x="{x + 96}" y="{y + 78}" font-family="PingFang SC, Source Han Sans SC, Arial, sans-serif" font-size="40" font-weight="950" fill="#F4F7FA">{escape(_fit_by_width(metric.value, 7))}</text>
        <text x="{x + 96}" y="{y + 110}" font-family="PingFang SC, Source Han Sans SC, Arial, sans-serif" font-size="23" font-weight="900" fill="{color}">{escape(_fit_by_width(metric.subvalue, 12))}</text>
      </g>"""
        )
    parts.append("</g>")
    return "".join(parts)


def _compact_metric_value(value: str, x: int, y: int, color: str) -> str:
    return f'<text x="{x}" y="{y}" text-anchor="middle" font-family="Helvetica Neue Condensed Black, Arial, sans-serif" font-size="25" font-weight="950" fill="{color}">{escape(_fit_by_width(value, 12))}</text>'


def _compact_key_metrics(metrics: tuple[Metric, ...]) -> str:
    xs = [82, 200, 318, 436]
    parts = ['<g class="compact-key-metric-strip key-metric-centered">']
    for index, metric in enumerate(metrics[:4]):
        x = xs[index]
        color = metric.color
        if index > 0:
            parts.append(f'<path d="M{x - 38} 706 V806" stroke="#3F7894" stroke-opacity="0.70" stroke-width="1"/>')
        parts.append(
            f"""
      <g>
        {_outline_icon_svg(metric.icon or "spark", x - 17, 700, 34, color)}
        <text x="{x}" y="762" text-anchor="middle" font-family="PingFang SC, Source Han Sans SC, Arial, sans-serif" font-size="15.5" font-weight="760" fill="#F4F7FA">{escape(_fit_by_width(metric.label, 8))}</text>
        {_compact_metric_value(metric.value, x, 800, color)}
      </g>"""
        )
    parts.append("</g>")
    return "".join(parts)


def _compact_highlight(data: PosterData) -> str:
    return f"""
    <g class="highlight-contained">
      <text x="44" y="936" font-family="PingFang SC, Source Han Sans SC, Arial, sans-serif" font-size="29" font-weight="950" fill="#A6FF38">{escape(_fit_by_width(data.highlight, 14))}</text>
      {_svg_multiline_text(data.insights, 44, 966, 31, 4, 18, font_size=13.4, weight=720)}
      {_activity_icon_svg("run", 328, 916, 42, "#8AF042")}
      {_activity_icon_svg("bike", 388, 916, 42, "#FFD21E")}
      {_activity_icon_svg("swim", 448, 916, 42, "#32B9F2")}
      <path d="M318 998 C356 972, 414 982, 486 956" fill="none" stroke="#32B9F2" stroke-opacity="0.18" stroke-width="3"/>
    </g>"""


def _compact_trend_lines(insights: str, recommendations: tuple[str, ...]) -> str:
    lines = [insights]
    lines.extend(recommendations[:3])
    icons = ["trend", "warning", "battery", "sleep"]
    colors = ["#8AF042", "#FFD21E", "#8AF042", "#30BDF2"]
    parts = []
    for index, line in enumerate(lines[:4]):
        y = 1130 + index * 63
        parts.append(
            f"""
      <g class="trend-separated">
        <circle cx="62" cy="{y - 12}" r="18" fill="{colors[index]}" fill-opacity="0.16"/>
        {_outline_icon_svg(icons[index], 47, y - 27, 29, colors[index])}
        {_svg_multiline_text(line, 92, y - 8, 45, 2, 18, font_size=13.8, weight=680)}
      </g>"""
        )
    return "".join(parts)


def _compact_timeline(days: tuple[TimelineDay, ...]) -> str:
    x = 570
    w = 430
    line_x = 555
    y = 64
    gap = 8
    cards: list[str] = []
    for _index, day in enumerate(days[:7]):
        is_hardest = day.tone == "hardest"
        h = _compact_day_height(day)
        accent = _timeline_accent(day.tone)
        badge = "#F1CF35" if is_hardest else ("#43D899" if day.tone != "recovery" else "#43D899")
        stroke = "#F1CF35" if is_hardest else ("#FF6570" if day.tone == "warning" else "#3F7894")
        details = _compact_hardest_details(day, x, y, w) if is_hardest else _compact_day_details(day, x, y, w)
        side_icon = (
            f'<g class="timeline-side-activity-icons">{_outline_icon_svg("warning", x + w - 58, y + h - 62, 42, "#FF6570")}</g>'
            if is_hardest
            else f'<g class="timeline-side-activity-icons">{_activity_icon_svg(day.icon or "run", x + w - 72, y + 34, 58, accent)}</g>'
        )
        star = f'<text x="804" y="{y + 58}" font-family="Helvetica Neue, Arial, sans-serif" font-size="20" font-weight="950" fill="#FFD21E">★</text>' if is_hardest else ""
        cards.append(
            f"""
    <g class="timeline-day-card" filter="url(#cardShadow)">
      <circle class="neon-timeline-nodes" cx="{line_x}" cy="{y + 20}" r="18" fill="{accent}" fill-opacity="0.16" filter="url(#nodeGlow)"/>
      <circle class="neon-timeline-nodes" cx="{line_x}" cy="{y + 20}" r="11" fill="{accent}" fill-opacity="0.34"/>
      <circle cx="{line_x}" cy="{y + 20}" r="7" fill="{accent}" stroke="#D7FBFF" stroke-opacity="0.82" stroke-width="2"/>
      <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="url(#cardGlass)" stroke="{stroke}" stroke-opacity="{0.90 if is_hardest else 0.72}" stroke-width="{1.4 if is_hardest else 1}"/>
      <rect x="{x}" y="{y}" width="{w}" height="50" rx="12" fill="url(#cardTop)" opacity="0.66"/>
      <rect x="{x + 12}" y="{y + 12}" width="58" height="30" rx="7" fill="{badge}"/>
      <text x="{x + 41}" y="{y + 34}" text-anchor="middle" font-family="PingFang SC, Source Han Sans SC, Arial, sans-serif" font-size="18" font-weight="950" fill="#061426">{escape(day.day)}</text>
      <rect x="{x + 76}" y="{y + 12}" width="66" height="30" rx="7" fill="#213A53"/>
      <text x="{x + 109}" y="{y + 34}" text-anchor="middle" font-family="Helvetica Neue, Arial, sans-serif" font-size="18" font-weight="900" fill="#F4F7FA">{escape(day.date)}</text>
      {side_icon}
      {_activity_icon_svg(day.icon or "run", x + 18, y + 58, 42, accent)}
      <text x="{x + 66}" y="{y + 84}" font-family="PingFang SC, Source Han Sans SC, Arial, sans-serif" font-size="25" font-weight="950" fill="{accent if is_hardest else '#F4F7FA'}">{escape(_fit_by_width(day.title, 22))}</text>
      {star}
      <path d="M{x + 20} {y + 100} H{x + w - 18}" stroke="#A7C7D8" stroke-opacity="0.20" stroke-width="1"/>
      {details}
    </g>"""
        )
        y += h + gap
    return f"""
    <g class="timeline-column-44">
      <path d="M{line_x} 42 V1418" stroke="#3FD6E0" stroke-opacity="0.70" stroke-width="2"/>
      {''.join(cards)}
    </g>"""


def _compact_day_height(day: TimelineDay) -> int:
    if day.tone == "hardest":
        return 286
    detail_count = len(day.details)
    if detail_count >= 5:
        return 206
    if detail_count >= 4:
        return 192
    if detail_count >= 3:
        return 168
    if detail_count >= 2:
        return 148
    return 136


def _compact_day_details(day: TimelineDay, x: int, y: int, w: int) -> str:
    parts = []
    split_after = max(1, min(2, len(day.details)))
    detail_count = min(len(day.details), 5)
    if detail_count == 0:
        return ""
    row_gap = 21
    content_top = y + 112
    content_bottom = y + _compact_day_height(day) - 18
    total_rows_height = (detail_count - 1) * row_gap
    first_row_y = int(content_top + max(0, content_bottom - content_top - total_rows_height) / 2) + 4
    for index, detail in enumerate(day.details[:5]):
        row_y = first_row_y + index * row_gap
        bullet = "#FFD21E" if day.tone == "warning" else ("#8AF042" if index < split_after else "#D5F7FF")
        if index == split_after:
            parts.append(f'<path d="M{x + 20} {row_y - 16} H{x + w - 18}" stroke="#A7C7D8" stroke-opacity="0.16" stroke-width="1"/>')
        parts.append(
            f'<g class="timeline-details-centered"><circle cx="{x + 62}" cy="{row_y - 6}" r="3" fill="{bullet}"/><text x="{x + 74}" y="{row_y}" font-family="PingFang SC, Source Han Sans SC, Arial, sans-serif" font-size="12.6" font-weight="650" fill="#F4F7FA">{escape(_fit_by_width(detail, 50))}</text></g>'
        )
    return "".join(parts)


def _compact_hardest_details(day: TimelineDay, x: int, y: int, w: int) -> str:
    rows = []
    for index, detail in enumerate(day.details[:3]):
        sport, color = _detail_sport(detail, index)
        label = {"run": "跑步", "bike": "骑行", "swim": "游泳", "tri": "铁三"}.get(sport, "训练")
        text = detail.split("：", 1)[-1] if "：" in detail else detail.split(" ", 1)[-1]
        row_y = y + 112 + index * 42
        row_icon = "run" if sport == "tri" else sport
        rows.append(
            f"""
      <g class="hardest-day-sport-breakdown">
        <rect x="{x + 18}" y="{row_y}" width="{w - 36}" height="34" rx="7" fill="#071D30" fill-opacity="0.72" stroke="{color}" stroke-opacity="0.25"/>
        {_activity_icon_svg(row_icon, x + 26, row_y + 3, 28, color)}
        <text x="{x + 72}" y="{row_y + 23}" font-family="PingFang SC, Source Han Sans SC, Arial, sans-serif" font-size="16" font-weight="900" fill="{color}">{escape(label)}</text>
        <path d="M{x + 126} {row_y + 7} V{row_y + 27}" stroke="#A7C7D8" stroke-opacity="0.25"/>
        {_svg_multiline_text(text, x + 142, row_y + 14, 37, 2, 13, font_size=10.8, weight=650)}
      </g>"""
        )
    warnings = day.details[3:5]
    warning_text = " / ".join(warnings) if warnings else "睡眠 74（仅 5.7 小时） / 训练准备度 14（POOR）"
    rows.append(
        f"""
      <path d="M{x + 20} {y + 244} H{x + w - 72}" stroke="#FF6570" stroke-opacity="0.32" stroke-width="1"/>
      {_svg_multiline_text(warning_text, x + 24, y + 264, 46, 2, 18, font_size=12.4, weight=850, fill="#FF6570")}"""
    )
    return "".join(rows)


def render_svg(data: PosterData) -> str:
    headline = data.headline
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">
  <!-- modern-sports-dashboard-poster canvas-1024x1536 summary-column-52 timeline-column-44 material-outline-icons embedded-png-icon-assets large-number-priority minimal-premium-layout reference-polished-layout 1024x1536-compact-density header-pill-clear-spacing premium-poster-depth precision-grid-background glass-card-inner-glow neon-timeline-nodes crisp-text-alignment -->
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#05192B"/>
      <stop offset="55%" stop-color="#071C30"/>
      <stop offset="100%" stop-color="#0B2740"/>
    </linearGradient>
    <radialGradient id="edgeGlow" cx="50%" cy="50%" r="70%">
      <stop offset="0%" stop-color="#071C30" stop-opacity="0"/>
      <stop offset="100%" stop-color="#42E6C1" stop-opacity="0.12"/>
    </radialGradient>
    <linearGradient id="cardGlass" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#0A2538" stop-opacity="0.88"/>
      <stop offset="100%" stop-color="#071D30" stop-opacity="0.78"/>
    </linearGradient>
    <linearGradient id="cardTop" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#18364A" stop-opacity="0.82"/>
      <stop offset="100%" stop-color="#071D30" stop-opacity="0.62"/>
    </linearGradient>
    <linearGradient id="cardInnerGlow" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#35E7C2" stop-opacity="0.08"/>
      <stop offset="38%" stop-color="#32B9F2" stop-opacity="0.035"/>
      <stop offset="100%" stop-color="#001217" stop-opacity="0"/>
    </linearGradient>
    <linearGradient id="posterSweep" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#35E7C2" stop-opacity="0.16"/>
      <stop offset="52%" stop-color="#0B3046" stop-opacity="0"/>
      <stop offset="100%" stop-color="#A6FF38" stop-opacity="0.10"/>
    </linearGradient>
    <filter id="softShadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="10" stdDeviation="10" flood-color="#001217" flood-opacity="0.48"/>
    </filter>
    <filter id="cardShadow" x="-12%" y="-12%" width="124%" height="124%">
      <feDropShadow dx="0" dy="8" stdDeviation="9" flood-color="#001217" flood-opacity="0.40"/>
    </filter>
    <filter id="neonGlow" x="-40%" y="-40%" width="180%" height="180%">
      <feGaussianBlur stdDeviation="3" result="coloredBlur"/>
      <feMerge>
        <feMergeNode in="coloredBlur"/>
        <feMergeNode in="SourceGraphic"/>
      </feMerge>
    </filter>
    <filter id="nodeGlow" x="-120%" y="-120%" width="340%" height="340%">
      <feGaussianBlur stdDeviation="7" result="coloredBlur"/>
      <feMerge>
        <feMergeNode in="coloredBlur"/>
        <feMergeNode in="SourceGraphic"/>
      </feMerge>
    </filter>
  </defs>
  <rect width="{WIDTH}" height="{HEIGHT}" fill="url(#bg)"/>
  <rect width="{WIDTH}" height="{HEIGHT}" fill="url(#edgeGlow)"/>
  <rect width="{WIDTH}" height="{HEIGHT}" fill="url(#posterSweep)" opacity="0.42"/>
  <g class="precision-grid-background premium-poster-depth" opacity="0.105">
    <path d="M64 0 V1418 M128 0 V1418 M192 0 V1418 M256 0 V1418 M320 0 V1418 M384 0 V1418 M448 0 V1418 M512 0 V1418 M576 0 V1418 M640 0 V1418 M704 0 V1418 M768 0 V1418 M832 0 V1418 M896 0 V1418 M960 0 V1418" stroke="#3A7894" stroke-width="1"/>
    <path d="M0 96 H1024 M0 192 H1024 M0 288 H1024 M0 384 H1024 M0 480 H1024 M0 576 H1024 M0 672 H1024 M0 768 H1024 M0 864 H1024 M0 960 H1024 M0 1056 H1024 M0 1152 H1024 M0 1248 H1024 M0 1344 H1024" stroke="#3A7894" stroke-width="1"/>
  </g>
  <g class="precision-grid-background" opacity="0.13">
    <circle cx="678" cy="24" r="1.7" fill="#3FD6E0"/><circle cx="714" cy="24" r="1.7" fill="#3FD6E0"/><circle cx="750" cy="24" r="1.7" fill="#3FD6E0"/><circle cx="786" cy="24" r="1.7" fill="#3FD6E0"/><circle cx="822" cy="24" r="1.7" fill="#3FD6E0"/><circle cx="858" cy="24" r="1.7" fill="#3FD6E0"/><circle cx="894" cy="24" r="1.7" fill="#3FD6E0"/><circle cx="930" cy="24" r="1.7" fill="#3FD6E0"/>
    <circle cx="678" cy="52" r="1.7" fill="#3FD6E0"/><circle cx="714" cy="52" r="1.7" fill="#3FD6E0"/><circle cx="750" cy="52" r="1.7" fill="#3FD6E0"/><circle cx="786" cy="52" r="1.7" fill="#3FD6E0"/><circle cx="822" cy="52" r="1.7" fill="#3FD6E0"/><circle cx="858" cy="52" r="1.7" fill="#3FD6E0"/><circle cx="894" cy="52" r="1.7" fill="#3FD6E0"/><circle cx="930" cy="52" r="1.7" fill="#3FD6E0"/>
    <circle cx="678" cy="80" r="1.7" fill="#3FD6E0"/><circle cx="714" cy="80" r="1.7" fill="#3FD6E0"/><circle cx="750" cy="80" r="1.7" fill="#3FD6E0"/><circle cx="786" cy="80" r="1.7" fill="#3FD6E0"/><circle cx="822" cy="80" r="1.7" fill="#3FD6E0"/><circle cx="858" cy="80" r="1.7" fill="#3FD6E0"/><circle cx="894" cy="80" r="1.7" fill="#3FD6E0"/><circle cx="930" cy="80" r="1.7" fill="#3FD6E0"/>
    <circle cx="678" cy="108" r="1.7" fill="#3FD6E0"/><circle cx="714" cy="108" r="1.7" fill="#3FD6E0"/><circle cx="750" cy="108" r="1.7" fill="#3FD6E0"/><circle cx="786" cy="108" r="1.7" fill="#3FD6E0"/><circle cx="822" cy="108" r="1.7" fill="#3FD6E0"/><circle cx="858" cy="108" r="1.7" fill="#3FD6E0"/><circle cx="894" cy="108" r="1.7" fill="#3FD6E0"/><circle cx="930" cy="108" r="1.7" fill="#3FD6E0"/>
  </g>
  <path d="M0 1424 H1024" stroke="#3FD6E0" stroke-opacity="0.50" stroke-width="1.2"/>
  <path d="M530 0 V1536" stroke="#3FD6E0" stroke-opacity="0.05" stroke-width="1"/>
  <path d="M44 260 H988 M44 710 H988 M44 1160 H988" stroke="#3F7894" stroke-opacity="0.045"/>
  <g opacity="0.18">
    <path d="M704 -6 C650 48 548 70 442 138 S548 214 424 254" fill="none" stroke="#A6FF38" stroke-width="3.2" stroke-linecap="round" filter="url(#neonGlow)"/>
    <path d="M755 -16 C772 62 706 110 704 170 S620 218 684 254" fill="none" stroke="#3FD6E0" stroke-width="3" stroke-linecap="round"/>
    <circle cx="704" cy="50" r="12" fill="#42E6C1"/>
    <circle cx="704" cy="50" r="4" fill="#05192B"/>
  </g>
  <g opacity="0.16">
    <circle cx="746" cy="32" r="2" fill="#3FD6E0"/><circle cx="782" cy="32" r="2" fill="#3FD6E0"/><circle cx="818" cy="32" r="2" fill="#3FD6E0"/><circle cx="854" cy="32" r="2" fill="#3FD6E0"/><circle cx="890" cy="32" r="2" fill="#3FD6E0"/>
    <circle cx="746" cy="62" r="2" fill="#3FD6E0"/><circle cx="782" cy="62" r="2" fill="#3FD6E0"/><circle cx="818" cy="62" r="2" fill="#3FD6E0"/><circle cx="854" cy="62" r="2" fill="#3FD6E0"/><circle cx="890" cy="62" r="2" fill="#3FD6E0"/>
    <circle cx="746" cy="92" r="2" fill="#3FD6E0"/><circle cx="782" cy="92" r="2" fill="#3FD6E0"/><circle cx="818" cy="92" r="2" fill="#3FD6E0"/><circle cx="854" cy="92" r="2" fill="#3FD6E0"/><circle cx="890" cy="92" r="2" fill="#3FD6E0"/>
  </g>
  <g filter="url(#softShadow)" class="summary-column-52 crisp-text-alignment">
    <text x="30" y="60" font-family="Helvetica Neue Condensed Black, Helvetica Neue, Arial, sans-serif" font-size="38" font-weight="900" fill="#F4F7FA">{escape(data.title)}</text>
    <rect x="222" y="30" width="188" height="36" rx="6" fill="#071D30" stroke="#35E7B8" stroke-width="2"/>
    <text x="316" y="55" text-anchor="middle" font-family="Helvetica Neue, Arial, sans-serif" font-size="20" font-weight="900" fill="#35E7B8">{escape(data.period)}</text>
    <text x="33" y="140" font-family="PingFang SC, Source Han Sans SC, Arial, sans-serif" font-size="68" font-weight="950" fill="#001217" fill-opacity="0.62">{escape(data.main_title)}</text>
    <text x="30" y="137" font-family="PingFang SC, Source Han Sans SC, Arial, sans-serif" font-size="68" font-weight="950" fill="#F4F7FA">{escape(data.main_title)}</text>
    <text x="30" y="186" font-family="PingFang SC, Source Han Sans SC, Arial, sans-serif" font-size="24" font-weight="900" fill="#44E7B6">{escape(data.date_range)}</text>
    <text x="30" y="244" font-family="Bradley Hand, Marker Felt, Avenir Next Condensed, Arial, sans-serif" font-size="30" font-style="italic" font-weight="800" fill="#A6FF38">{escape(headline)}</text>
    <path d="M42 263 C116 272, 230 250, 358 258" stroke="#A6FF38" stroke-width="3" stroke-linecap="round" fill="none"/>
  </g>

  <g filter="url(#cardShadow)">
    {_compact_card(22, 290, 486, 334)}
    <desc>Summary Dashboard</desc>
    {_compact_heading("总量概览", "bars", 42, 327, "#F4F7FA")}
    {_compact_summary_grid(data.weekly_totals)}
  </g>

  <g filter="url(#cardShadow)">
    {_compact_card(22, 640, 486, 188)}
    {_compact_heading("关键数据", "spark", 42, 677, "#FFD21E")}
    {_compact_key_metrics(data.key_metrics)}
  </g>

  <g filter="url(#softShadow)">
    {_compact_card(22, 848, 486, 178, accent="#3F7894")}
    {_compact_heading("本周亮点", "trophy", 42, 887, "#FFD21E")}
    {_compact_highlight(data)}
  </g>

  <g filter="url(#cardShadow)">
    {_compact_card(22, 1042, 486, 358)}
    {_compact_heading("趋势与建议", "trend", 42, 1083, "#8AF042")}
    {_compact_trend_lines(data.insights, data.recommendations)}
  </g>

  <desc>Timeline</desc>
  {_compact_timeline(data.timeline)}

  {_outline_icon_svg("swoosh", 42, 1456, 76, "#F4F7FA")}
  <text x="134" y="1474" font-family="Helvetica Neue Condensed Black, Helvetica Neue, Arial, sans-serif" font-size="20" font-weight="950" font-style="italic" fill="#F4F7FA">{escape(_fit_by_width(data.quote.split(',')[0], 26))},</text>
  <text x="134" y="1500" font-family="Helvetica Neue Condensed Black, Helvetica Neue, Arial, sans-serif" font-size="20" font-weight="950" font-style="italic" fill="#A6FF38">BIG CHANGES.</text>
  <path d="M404 1456 H516 L486 1528" fill="none" stroke="#32B9F2" stroke-opacity="0.18"/>
  <path d="M478 1532 L535 1452" stroke="#A6FF38" stroke-width="18"/>
  <path d="M452 1532 L508 1452" stroke="#3FD6E0" stroke-opacity="0.52" stroke-width="12"/>
  <text x="626" y="1508" font-family="Helvetica Neue, Arial, sans-serif" font-size="16" font-weight="900" font-style="italic" fill="#6CBFEE" fill-opacity="0.34" letter-spacing="7">YOU VS. YOU. EVERY DAY.</text>
</svg>
"""


def _convert_svg_to_png(svg_path: Path, output_path: Path) -> None:
    sips = shutil.which("sips")
    if sips is None:
        raise RuntimeError("sips is required to convert SVG to PNG on this host")
    completed = subprocess.run(
        [sips, "-s", "format", "png", str(svg_path), "--out", str(output_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "sips failed to create PNG")


def default_output_path() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return DEFAULT_OUTBOUND_DIR / f"training-summary-{stamp}.png"


def generate_poster(payload: dict[str, Any], output_path: Path | None = None) -> Path:
    data = normalize_poster_data(payload)
    output = (output_path or default_output_path()).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    svg = render_svg(data)
    with tempfile.TemporaryDirectory() as tmp:
        svg_path = Path(tmp) / "poster.svg"
        svg_path.write_text(svg, encoding="utf-8")
        _convert_svg_to_png(svg_path, output)
    return output


def build_result(output_path: Path, message: str | None = None) -> dict[str, Any]:
    media_urls = [str(output_path.resolve())]
    result: dict[str, Any] = {
        "ok": True,
        "path": media_urls[0],
        "mediaUrls": media_urls,
        "width": WIDTH,
        "height": HEIGHT,
        "format": "png",
    }
    if message is not None:
        result["richPayload"] = {
            "message": message,
            "mediaUrls": media_urls,
        }
    return result


def _load_json(path: str | None) -> dict[str, Any]:
    raw = sys.stdin.read() if path in (None, "-") else Path(path).read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("poster input must be a JSON object")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="JSON file; omit or use '-' for stdin")
    parser.add_argument("--output", help="Output PNG path")
    parser.add_argument("--message-file", help="Markdown/text report to include in richPayload")
    args = parser.parse_args(argv)

    try:
        payload = _load_json(args.input)
        output = generate_poster(
            payload,
            Path(args.output) if args.output else None,
        )
        message = (
            Path(args.message_file).read_text(encoding="utf-8")
            if args.message_file
            else None
        )
        print(json.dumps(build_result(output, message), ensure_ascii=False))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
