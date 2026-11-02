"""Small, dependency-free stdio MCP adapter for private training workflows.

Only allowlisted operations are callable by a model host. The Garmin session,
database location, and raw evidence never become tool inputs.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import TextIO

from athlete_growth.interfaces.host import capabilities as host_capabilities
from athlete_growth.interfaces.host import planning as planning_tools
from athlete_growth.interfaces.host import tools as host_tools
from athlete_growth.interfaces.mcp import capabilities as capability_mcp

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "athlete-growth"
SERVER_VERSION = "0.3.0"
SERVER_INSTRUCTIONS = (
    "Use this local athlete's MCP tools for evidence-backed training conversations. "
    "Read onboarding, sync and report status before planning. Treat missing check-ins "
    "as unknown readiness, not safety clearance. Preview generated goals, plans and "
    "check-ins, daily decisions and adaptations and show exact fields before confirmation; "
    "never call a confirm tool without the user's explicit approval. Feedback is "
    "user-reported; adaptations never override safety rules or silently change plans. "
    "Do not request Garmin credentials in chat. "
    "Recovery and nutrition outputs are general wellness guidance, not medical care."
)

_READ_TOOLS: dict[str, tuple[str, dict[str, object]]] = {
    "get_sync_status": (
        "Check device-source sync health, last run and pending analysis. "
        "Use this when the user asks whether their data is current.",
        {},
    ),
    "get_today_report": (
        "Read the athlete's evidence-backed daily training and wellness report. "
        "Use this for today's advice; explain uncertainty and safety flags.",
        {
            "date": {
                "type": "string",
                "format": "date",
                "description": "Local calendar date (YYYY-MM-DD); omit for today.",
            }
        },
    ),
    "get_weekly_report": (
        "Read the athlete's evidence-backed seven-day training report and trends. "
        "Use this for weekly review or load analysis.",
        {
            "end_date": {
                "type": "string",
                "format": "date",
                "description": "Last local calendar date (YYYY-MM-DD); omit for today.",
            }
        },
    ),
    "list_goals": (
        "List saved sport, body and race goals with conflicts. "
        "Use this before recommending a plan.",
        {},
    ),
    "list_notifications": (
        "Read unread local training-analysis notifications. "
        "Use this when the user asks what changed since their last conversation.",
        {},
    ),
}

_PLANNING_TOOLS: dict[str, tuple[str, dict[str, object], tuple[str, ...]]] = {
    "list_open_planning_drafts": (
        "Resume unfinished goal proposals and plan previews from this private profile.",
        {},
        (),
    ),
    "propose_goal": (
        "Create or revise a goal draft from the user's own idea. Review missing fields "
        "and assumptions with the user before calling confirm_goal.",
        {
            "idea": {"type": "string"},
            "proposal_id": {"type": "string"},
            "expected_revision": {"type": "integer"},
            "goal_type": {"type": "string", "enum": ["sport", "body", "race"]},
            "title": {"type": "string"},
            "priority": {"type": "integer", "minimum": 1, "maximum": 5},
            "deadline": {"type": "string", "format": "date"},
            "target_metric": {"type": "string"},
            "target_value": {"type": "number"},
        },
        (),
    ),
    "confirm_goal": (
        "Save the latest goal draft as an active goal only after the user explicitly "
        "approves its displayed fields and assumptions.",
        {"proposal_id": {"type": "string"}, "revision": {"type": "integer"}},
        ("proposal_id", "revision"),
    ),
    "preview_goal_change": (
        "Preview replacing or deleting an active goal and show affected plans. "
        "No goal changes yet.",
        {
            "action": {"type": "string", "enum": ["replace", "delete"]},
            "goal_id": {"type": "string"},
            "reason": {"type": "string"},
            "goal_type": {"type": "string", "enum": ["sport", "body", "race"]},
            "title": {"type": "string"},
            "priority": {"type": "integer", "minimum": 1, "maximum": 5},
            "deadline": {"type": "string", "format": "date"},
            "target_metric": {"type": "string"},
            "target_value": {"type": "number"},
        },
        ("action", "goal_id", "reason"),
    ),
    "confirm_goal_change": (
        "Apply a displayed goal replacement or deletion only after explicit user approval. "
        "Related plans require review.",
        {"preview_id": {"type": "string"}, "revision": {"type": "integer"}},
        ("preview_id", "revision"),
    ),
    "preview_plan": (
        "Create a private seven-day plan preview for a confirmed goal. Explain safety "
        "and data gaps; this does not approve the plan.",
        {
            "goal_id": {"type": "string"},
            "start_date": {"type": "string", "format": "date"},
            "available_minutes_by_weekday": {
                "type": "object",
                "additionalProperties": {"type": "integer", "minimum": 0},
                "description": "Weekday 0=Monday through 6=Sunday, mapped to available minutes.",
            },
            "max_weekly_minutes": {"type": "integer", "minimum": 1},
            "sport": {"type": "string", "enum": ["running", "cycling", "swimming"]},
            "sports": {
                "type": "array",
                "minItems": 2,
                "maxItems": 3,
                "uniqueItems": True,
                "items": {"type": "string", "enum": ["running", "cycling", "swimming"]},
            },
            "unavailable_weekdays": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0, "maximum": 6},
            },
            "preferred_quality_weekday": {"type": "integer", "minimum": 0, "maximum": 6},
            "preferred_long_weekday": {"type": "integer", "minimum": 0, "maximum": 6},
            "focus_sport": {"type": "string", "enum": ["running", "cycling", "swimming"]},
            "aerobic_mixing": {"type": "boolean"},
            "use_saved_preferences": {"type": "boolean"},
        },
        ("goal_id", "start_date", "available_minutes_by_weekday", "max_weekly_minutes"),
    ),
    "confirm_plan": (
        "Save a previously displayed plan preview only after the user explicitly "
        "approves it. Stale previews are rejected.",
        {"preview_id": {"type": "string"}, "revision": {"type": "integer"}},
        ("preview_id", "revision"),
    ),
    "review_plan": (
        "Check an approved plan for goal changes, new training data and a nearby deadline; "
        "return reasons for replanning without modifying it.",
        {"plan_id": {"type": "string"}},
        ("plan_id",),
    ),
    "preview_plan_update": (
        "Create a revised seven-day plan draft from an approved plan's saved constraints and "
        "current evidence. Show changes before confirmation.",
        {
            "plan_id": {"type": "string"},
            "goal_id": {"type": "string"},
            "start_date": {"type": "string", "format": "date"},
        },
        ("plan_id",),
    ),
}

_PLANNING_TOOLS["preview_plan_blocks"] = (
    "Preview a configurable multiweek sport-focus rotation. Only the first seven days "
    "become a confirmable plan draft; later weeks are structural forecasts.",
    {
        **_PLANNING_TOOLS["preview_plan"][1],
        "weeks": {"type": "integer", "minimum": 2, "maximum": 12},
        "focus_sequence": {
            "type": "array",
            "minItems": 1,
            "maxItems": 12,
            "items": {"type": "string", "enum": ["running", "cycling", "swimming"]},
        },
    },
    (*_PLANNING_TOOLS["preview_plan"][2], "weeks"),
)

_PLANNING_TOOLS["preview_plan_adjustment"] = (
    "Preview a user-requested skip, shorter session, easy sport substitution or move "
    "within the current week. Never changes an approved plan until confirm_plan.",
    {
        "plan_id": {"type": "string"},
        "session_id": {"type": "string"},
        "action": {"type": "string", "enum": ["skip", "shorten", "substitute", "move"]},
        "reason": {"type": "string", "maxLength": 300},
        "new_date": {"type": "string", "format": "date"},
        "new_sport": {"type": "string", "enum": ["running", "cycling", "swimming"]},
        "new_duration_min": {"type": "integer", "minimum": 1},
    },
    ("plan_id", "session_id", "action", "reason"),
)


def tool_catalog() -> list[dict[str, object]]:
    """Expose narrow read and draft/confirm tools with honest annotations."""

    reads = [
        {
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        }
        for name, (description, properties) in _READ_TOOLS.items()
    ]
    planning = [
        {
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": list(required),
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": name in {"list_open_planning_drafts", "review_plan"},
                "destructiveHint": False,
            },
        }
        for name, (description, properties, required) in _PLANNING_TOOLS.items()
    ]
    return reads + planning + capability_mcp.tool_catalog()


def call_tool(name: str, arguments: Mapping[str, object], data_home: Path) -> dict[str, object]:
    """Dispatch an allowlisted read or planning use case."""

    if name in _PLANNING_TOOLS:
        return _call_planning_tool(name, arguments, data_home)
    if name in capability_mcp.TOOL_SPECS:
        return capability_mcp.call_tool(name, arguments, data_home)
    selected = _READ_TOOLS.get(name)
    if selected is None:
        raise ValueError("unknown tool")
    _, allowed = selected
    if set(arguments) - set(allowed):
        raise ValueError("unsupported tool argument")
    if name == "get_sync_status":
        return host_tools.automation_status(data_home)
    if name == "get_today_report":
        value = arguments.get("date")
        if value is not None and not isinstance(value, str):
            raise ValueError("date must be a string")
        return host_capabilities.daily_report(
            data_home, date.fromisoformat(value) if value is not None else None
        )
    if name == "get_weekly_report":
        value = arguments.get("end_date")
        if value is not None and not isinstance(value, str):
            raise ValueError("end_date must be a string")
        return host_capabilities.weekly_report(
            data_home, date.fromisoformat(value) if value is not None else None
        )
    if name == "list_goals":
        return host_capabilities.goals(data_home)
    return host_tools.list_notifications(data_home)


def _call_planning_tool(
    name: str, arguments: Mapping[str, object], data_home: Path
) -> dict[str, object]:
    _, allowed, required = _PLANNING_TOOLS[name]
    if set(arguments) - set(allowed) or set(required) - set(arguments):
        raise ValueError("unsupported or missing tool argument")
    if name == "list_open_planning_drafts":
        return planning_tools.list_open_drafts(data_home)
    if name == "propose_goal":
        if "idea" not in arguments and "proposal_id" not in arguments:
            raise ValueError("idea or proposal_id is required")
        return planning_tools.propose_goal(
            data_home,
            idea=_optional_string(arguments, "idea"),
            proposal_id=_optional_string(arguments, "proposal_id"),
            expected_revision=_optional_int(arguments, "expected_revision"),
            goal_type=_optional_string(arguments, "goal_type"),
            title=_optional_string(arguments, "title"),
            priority=_optional_int(arguments, "priority"),
            deadline=_optional_date(arguments, "deadline"),
            target_metric=_optional_string(arguments, "target_metric"),
            target_value=_optional_float(arguments, "target_value"),
        )
    if name == "confirm_goal":
        return planning_tools.confirm_goal(
            data_home,
            _required_string(arguments, "proposal_id"),
            _required_int(arguments, "revision"),
        )
    if name == "preview_goal_change":
        return planning_tools.preview_goal_change(
            data_home,
            action=_required_string(arguments, "action"),
            goal_id=_required_string(arguments, "goal_id"),
            reason=_required_string(arguments, "reason"),
            goal_type=_optional_string(arguments, "goal_type"),
            title=_optional_string(arguments, "title"),
            priority=_optional_int(arguments, "priority"),
            deadline=_optional_date(arguments, "deadline"),
            target_metric=_optional_string(arguments, "target_metric"),
            target_value=_optional_float(arguments, "target_value"),
        )
    if name == "confirm_goal_change":
        return planning_tools.confirm_goal_change(
            data_home,
            _required_string(arguments, "preview_id"),
            _required_int(arguments, "revision"),
        )
    if name == "review_plan":
        return planning_tools.review_plan(data_home, _required_string(arguments, "plan_id"))
    if name == "preview_plan_update":
        return planning_tools.preview_plan_update(
            data_home,
            plan_id=_required_string(arguments, "plan_id"),
            goal_id=_optional_string(arguments, "goal_id"),
            start_date=_optional_date(arguments, "start_date"),
        )
    if name == "preview_plan_adjustment":
        return planning_tools.preview_plan_adjustment(
            data_home,
            plan_id=_required_string(arguments, "plan_id"),
            session_id=_required_string(arguments, "session_id"),
            action=_required_string(arguments, "action"),
            reason=_required_string(arguments, "reason"),
            new_date=_optional_date(arguments, "new_date"),
            new_sport=_optional_string(arguments, "new_sport"),
            new_duration_min=_optional_int(arguments, "new_duration_min"),
        )
    if name in {"preview_plan", "preview_plan_blocks"}:
        for field in ("preferred_quality_weekday", "preferred_long_weekday"):
            if field in arguments and (
                type(arguments[field]) is not int or not 0 <= arguments[field] <= 6
            ):
                raise ValueError("preferred weekdays must be between 0 and 6")
        if "aerobic_mixing" in arguments and type(arguments["aerobic_mixing"]) is not bool:
            raise ValueError("aerobic_mixing must be a boolean")
        if (
            "use_saved_preferences" in arguments
            and type(arguments["use_saved_preferences"]) is not bool
        ):
            raise ValueError("use_saved_preferences must be a boolean")
        raw_available = arguments["available_minutes_by_weekday"]
        if not isinstance(raw_available, dict):
            raise ValueError("availability must be an object")
        available: dict[int, int] = {}
        for key, value in raw_available.items():
            if not isinstance(key, str) or key not in {str(day) for day in range(7)}:
                raise ValueError("availability contains an invalid weekday")
            if type(value) is not int or value < 0:
                raise ValueError("availability minutes must be nonnegative integers")
            available[int(key)] = value
        raw_unavailable = arguments.get("unavailable_weekdays", [])
        if not isinstance(raw_unavailable, list) or any(
            type(day) is not int or not 0 <= day <= 6 for day in raw_unavailable
        ):
            raise ValueError("unavailable_weekdays must be weekdays 0-6")
        raw_sports = arguments.get("sports", [])
        if (
            not isinstance(raw_sports, list)
            or len(raw_sports) > 3
            or any(not isinstance(item, str) for item in raw_sports)
        ):
            raise ValueError("plan sports list is invalid")
        plan_inputs = dict(
            goal_id=_required_string(arguments, "goal_id"),
            start_date=date.fromisoformat(_required_string(arguments, "start_date")),
            available_minutes_by_weekday=available,
            max_weekly_minutes=_required_int(arguments, "max_weekly_minutes"),
            sport=_required_string(arguments, "sport") if "sport" in arguments else None,
            sports=tuple(raw_sports),
            unavailable_weekdays=tuple(raw_unavailable),
            preferred_quality_weekday=_optional_int(arguments, "preferred_quality_weekday"),
            preferred_long_weekday=_optional_int(arguments, "preferred_long_weekday"),
            focus_sport=_optional_string(arguments, "focus_sport"),
            aerobic_mixing=arguments.get("aerobic_mixing"),
            use_saved_preferences=arguments.get("use_saved_preferences", True),
        )
        if name == "preview_plan_blocks":
            raw_sequence = arguments.get("focus_sequence", [])
            if not isinstance(raw_sequence, list) or any(
                not isinstance(item, str) for item in raw_sequence
            ):
                raise ValueError("block focus sequence must use selected sports")
            return planning_tools.preview_plan_blocks(
                data_home,
                **plan_inputs,
                weeks=_required_int(arguments, "weeks"),
                focus_sequence=tuple(raw_sequence),
            )
        return planning_tools.preview_plan(data_home, **plan_inputs)
    return planning_tools.confirm_plan(
        data_home,
        _required_string(arguments, "preview_id"),
        _required_int(arguments, "revision"),
    )


def _required_string(arguments: Mapping[str, object], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a nonempty string")
    return value


def _optional_string(arguments: Mapping[str, object], key: str) -> str | None:
    return _required_string(arguments, key) if key in arguments else None


def _required_int(arguments: Mapping[str, object], key: str) -> int:
    value = arguments.get(key)
    if type(value) is not int or value < 1:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _optional_int(arguments: Mapping[str, object], key: str) -> int | None:
    return _required_int(arguments, key) if key in arguments else None


def _optional_date(arguments: Mapping[str, object], key: str) -> date | None:
    return date.fromisoformat(_required_string(arguments, key)) if key in arguments else None


def _optional_float(arguments: Mapping[str, object], key: str) -> float | None:
    if key not in arguments:
        return None
    value = arguments[key]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{key} must be a finite number")
    if not math.isfinite(value):
        raise ValueError(f"{key} must be a finite number")
    return float(value)


def handle_message(message: object, data_home: Path) -> dict[str, object] | None:
    """Handle a single newline-delimited MCP JSON-RPC message."""

    if not isinstance(message, dict):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }
    request_id = message.get("id")
    if request_id is None:  # MCP notifications do not receive responses.
        return None
    method = message.get("method")
    params = message.get("params", {})
    if not isinstance(params, dict):
        return _error(request_id, -32602, "Invalid params")
    if method == "initialize":
        result: dict[str, object] = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": SERVER_INSTRUCTIONS,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tool_catalog()}
    elif method == "tools/call":
        name, arguments = params.get("name"), params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _error(request_id, -32602, "Invalid tool call")
        try:
            payload = call_tool(name, arguments, data_home)
        except (
            KeyError,
            ValueError,
            RuntimeError,
            FileNotFoundError,
            OSError,
            sqlite3.Error,
            TypeError,
        ) as exc:
            # Filesystem paths, account details and Garmin exception text must not leak.
            safe_error = (
                _safe_planning_error(exc)
                if name in _PLANNING_TOOLS
                else _safe_capability_error(exc)
                if name in capability_mcp.TOOL_SPECS
                else None
            )
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": safe_error
                        or "Tool unavailable; check the local app and sync status.",
                    }
                ],
                "isError": True,
            }
            if safe_error:
                result["structuredContent"] = {"error": safe_error}
        else:
            if name in {
                "render_periodic_training_poster",
                "get_running_form_contact_sheet",
            } and payload.get("_image_base64"):
                encoded = payload.pop("_image_base64", None)
                if not isinstance(encoded, str):
                    return _error(request_id, -32603, "Image rendering failed")
                result = {
                    "content": [
                        {"type": "text", "text": json.dumps(payload, ensure_ascii=False)},
                        {"type": "image", "data": encoded, "mimeType": "image/png"},
                    ],
                    "structuredContent": payload,
                    "isError": False,
                }
            else:
                result = {
                    "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                    "structuredContent": payload,
                    "isError": False,
                }
    else:
        return _error(request_id, -32601, "Method not found")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def serve(reader: TextIO, writer: TextIO, data_home: Path) -> None:
    """Keep stdout reserved for MCP frames; malformed input cannot stop service."""

    for line in reader:
        try:
            response = handle_message(json.loads(line), data_home)
        except (TypeError, ValueError, json.JSONDecodeError):
            response = _error(None, -32700, "Parse error")
        if response is not None:
            writer.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            writer.flush()


def main() -> int:
    data_home = (
        Path(os.environ.get("ATHLETE_GROWTH_HOME", str(Path.home() / ".athlete-growth")))
        .expanduser()
        .resolve()
    )
    serve(sys.stdin, sys.stdout, data_home)
    return 0


def _error(request_id: object, code: int, message: str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _safe_planning_error(exc: Exception) -> str | None:
    """Expose only static product messages, never filesystem or connector details."""

    safe_messages = {
        "draft revision changed; review the latest draft",
        "goal draft is incomplete; review missing fields",
        "source data changed; preview the plan again",
        "safety context is stale; preview the plan again",
        "plan version changed; preview the plan again",
        "goal is no longer active; preview the plan again",
        "no sessions fit the available time and hard constraints",
        "plan start date cannot be in the past",
        "weekly minutes and at least one sport are required",
        "provide exactly one sport or sports list",
        "plan sports must be running, cycling or swimming",
        "plan sports must not repeat",
        "plan sports list is invalid",
        "goal_type must be sport, body, or race",
        "priority must be between 1 and 5",
        "target_value requires target_metric",
        "tell us what you want to work toward",
        "expected_revision is required when revising a goal",
        "confirmed goals cannot be revised as drafts",
        "draft changed or is no longer editable",
        "use confirm_goal_change for a goal change",
        "use confirm_goal for a new goal",
        "action must be replace or delete",
        "goal change reason is required",
        "goal change reason is too long",
        "replacement goal needs type and title",
        "replacement goal needs priority between 1 and 5",
        "replacement target requires a finite metric and value",
        "delete does not accept replacement fields",
        "goal changed; preview the goal change again",
        "source data changed; preview the goal change again",
        "preferred weekdays must be between 0 and 6",
        "focus sport must be among allowed sports",
        "aerobic_mixing must be a boolean",
        "use_saved_preferences must be a boolean",
        "previous plan has no reusable constraints",
        "choose an active goal for replanning",
        "block weeks must be between 2 and 12",
        "at least one block sport is required",
        "block focus sequence must use selected sports",
        "block rotation needs an explicit sequence or confirmed preference",
        "block cycle ended; preview new blocks",
        "block preference changed; preview blocks again",
        "block update must start on a forecast week boundary",
        "plan adjustment action is invalid",
        "past sessions cannot be edited; record the actual workout",
        "skip does not accept replacement fields",
        "shorten only accepts a smaller duration",
        "substitute sport must be in the plan",
        "substitute sport must differ from the planned sport",
        "substitute duration exceeds the conservative cap",
        "move requires only a new date",
        "moved session must stay in the current plan week",
        "moved session needs a different date",
        "moved session collides with another planned session",
        "moved session exceeds available time",
        "key sessions need a day between them",
        "plan adjustment reason is required and limited to 300 characters",
    }
    message = str(exc)
    if message in safe_messages:
        return message
    if isinstance(exc, KeyError) and message.strip("'") in {
        "draft not found",
        "confirmed goal not found",
        "confirmed plan not found",
    }:
        return message.strip("'")
    return None


def _safe_capability_error(exc: Exception) -> str | None:
    """Return only reviewed validation messages, never a path or source exception."""

    safe_messages = {
        "check-in changed; preview it again",
        "check-in timestamp must include a timezone",
        "check-in timestamp cannot be in the future",
        "check-in scores must be integers between 0 and 10",
        "check-in flags must be booleans",
        "limit must be between 1 and 50",
        "limit must be between 1 and 10",
        "body area and stable days are invalid",
        "pain must be between 0 and 10",
        "weight_kg must be between 20 and 400",
        "duration or intensity is invalid",
        "temperature is invalid",
        "sweat rate is invalid",
        "too many dietary entries",
        "race distance, elevation, or target time is invalid",
        (
            "quantitative race strategy currently supports running only; "
            "cycling, swimming and triathlon need discipline-specific inputs"
        ),
        "event name is invalid",
        "wind is invalid",
        "provide 3 to 100 finite numeric sample pairs",
        "provide 3 to 100 sample pairs",
        "each sample must contain two numbers",
        "expected a finite number",
        "unsupported or missing tool argument",
        "current activity record not found",
        "current training-plan record not found",
        "plan sessions are unavailable",
        "planned session not found",
        "sport or baseline window is invalid",
        "training load sport is invalid",
        "training load end date is in the future",
        "check-in history window is invalid",
        "race segments are invalid",
        "race segments do not match event sport",
        "race segment target is invalid",
        "race segment distance is invalid",
        "transition must not have distance",
        "fueling duration is invalid",
        "declared tested carb rate is invalid",
        "carbs per serving is invalid",
        "available servings is invalid",
        "fueling opportunities are invalid",
        "period anchor is in the future",
        "unsupported summary cycle",
        "no activities in period",
        "poster renderer unavailable",
        "poster renderer failed",
        "memory kind is invalid",
        "memory time must include a timezone",
        "memory time is in the future",
        "memory fields are invalid",
        "memory fields contain unsupported keys",
        "sport is invalid",
        "activity needs duration or distance",
        "sleep_hours is invalid",
        "illness must be boolean",
        "body status needs an observation",
        "memory note is required",
        "training memory changed; preview it again",
        "memory window or limit is invalid",
        "training preference fields are invalid",
        "preference weekdays must be between 0 and 6",
        "aerobic_mixing preference must be boolean",
        "focus_sport preference is invalid",
        "key_session_pattern preference is invalid",
        "training preferences changed; preview them again",
        "training preference version already exists",
        "invalid running-form job ID",
        "running-form analysis is not complete",
    }
    if isinstance(exc, KeyError) and str(exc).strip("'") == "running-form job not found":
        return "running-form job not found"
    return str(exc) if str(exc) in safe_messages else None


if __name__ == "__main__":
    raise SystemExit(main())
