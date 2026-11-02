"""Request MCP refresh through a reachable Codex app-server control socket."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def reload_codex_mcp(*, socket_path: Path | None = None) -> dict[str, object]:
    """Reload a connected Codex app-server; never claim the Desktop UI was refreshed."""
    command = ["codex", "app-server", "proxy"]
    if socket_path is not None:
        command.extend(("--sock", str(socket_path.expanduser().resolve())))
    requests = (
        {
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "athlete-growth-mcp-reload", "version": "1.0.0"}},
        },
        {"method": "initialized"},
        {"id": 2, "method": "config/mcpServer/reload", "params": None},
    )
    payload = "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in requests)
    try:
        completed = subprocess.run(
            command,
            input=payload,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Codex CLI is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Codex MCP reload timed out; current session was not verified") from exc
    if completed.returncode != 0:
        raise RuntimeError(
            "Codex app-server control socket is unavailable; this command cannot reload "
            "an embedded Desktop session"
        )
    replies = []
    for line in completed.stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            replies.append(value)
    response = next((item for item in replies if item.get("id") == 2), None)
    if response is None:
        raise RuntimeError("Codex app-server did not confirm the MCP reload request")
    if "error" in response:
        raise RuntimeError("Codex app-server rejected the MCP reload request")
    if "result" not in response:
        raise RuntimeError("Codex app-server returned an invalid MCP reload response")
    return {
        "reload_requested": True,
        "host": "connected Codex app-server",
        "current_desktop_task_verified": False,
        "next_step": "Start a new turn and check the expected MCP tool in that task.",
    }
