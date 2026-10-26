"""Validation helpers for identifiers used as filesystem path components."""

from __future__ import annotations

import re
from pathlib import Path

_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9._-]+", re.ASCII)


def require_safe_component(value: str, field_name: str) -> None:
    """Reject blank, nested, absolute, traversal, and non-portable components."""

    if (
        not isinstance(value, str)
        or value in {".", ".."}
        or _SAFE_COMPONENT.fullmatch(value) is None
    ):
        raise ValueError(f"{field_name} must be a safe path component")


def require_strict_child(root: Path, candidate: Path, field_name: str) -> Path:
    """Resolve a candidate and prove it remains strictly beneath the resolved root."""

    resolved_root = root.resolve(strict=False)
    resolved_candidate = candidate.resolve(strict=False)
    if resolved_candidate == resolved_root or not resolved_candidate.is_relative_to(resolved_root):
        raise ValueError(f"{field_name} must remain strictly beneath the raw root")
    return resolved_candidate
