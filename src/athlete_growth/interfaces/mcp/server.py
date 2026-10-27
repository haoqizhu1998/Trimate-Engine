"""Small, dependency-free stdio MCP adapter for private training reports.

Only the allowlisted read operations below are callable by a model host. The
Garmin session, database location, and raw evidence never become tool inputs.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import TextIO

from athlete_growth.interfaces.cli import commands
from athlete_growth.interfaces.cli.main import _build_parser

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "athlete-growth"
SERVER_VERSION = "0.1.0"

_TOOL_COMMANDS: dict[str, tuple[str, str, dict[str, object]]] = {
    "get_sync_status": (
        "automation-status",
        "Check Garmin China sync health, last run and pending analysis. "
        "Use this when the user asks whether their data is current.",
        {},
    ),
    "get_today_report": (
        "today",
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
        "weekly",
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
        "goal-list",
        "List saved sport, body and race goals with conflicts. "
        "Use this before recommending a plan.",
        {},
    ),
    "list_notifications": (
        "notification-list",
        "Read unread local training-analysis notifications. "
        "Use this when the user asks what changed since their last conversation.",
        {},
    ),
}


def tool_catalog() -> list[dict[str, object]]:
    """Expose a deliberately narrow and read-only public tool surface."""

    return [
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
        for name, (_, description, properties) in _TOOL_COMMANDS.items()
    ]


def call_tool(name: str, arguments: Mapping[str, object], data_home: Path) -> dict[str, object]:
    """Dispatch one allowlisted tool through the existing application CLI handler."""

    selected = _TOOL_COMMANDS.get(name)
    if selected is None:
        raise ValueError("unknown tool")
    command, _, allowed = selected
    if set(arguments) - set(allowed):
        raise ValueError("unsupported tool argument")
    argv = [command, "--data-home", str(data_home)]
    for key, value in arguments.items():
        if not isinstance(value, str):
            raise ValueError("date arguments must be strings")
        date.fromisoformat(value)
        argv.extend(("--" + key.replace("_", "-"), value))
    parsed = _build_parser().parse_args(argv)
    result = commands.execute(parsed)
    return json.loads(json.dumps(result, ensure_ascii=False, default=_json_default))


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
        except (KeyError, ValueError, RuntimeError, FileNotFoundError, OSError):
            # Filesystem paths, account details and Garmin exception text must not leak.
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": "Tool unavailable; check the local app and sync status.",
                    }
                ],
                "isError": True,
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


def _json_default(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


if __name__ == "__main__":
    raise SystemExit(main())
