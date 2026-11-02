"""Install this product's local MCP entry without replacing other WorkBuddy entries."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import cast

SERVER_NAME = "athlete-growth"


def configure_workbuddy(
    *, python_executable: Path, data_home: Path, config_path: Path | None = None
) -> dict[str, object]:
    path = config_path or Path.home() / ".workbuddy" / "mcp.json"
    document = _read_config(path)
    servers = cast(dict[str, object], document.setdefault("mcpServers", {}))
    entry: dict[str, object] = {
        "type": "stdio",
        "command": str(python_executable.absolute()),
        "args": ["-m", "athlete_growth.interfaces.mcp.server"],
        "env": {"ATHLETE_GROWTH_HOME": str(data_home.absolute())},
    }
    previous = servers.get(SERVER_NAME)
    if previous is not None and previous != entry and not _looks_like_our_entry(previous):
        raise ValueError("WorkBuddy already has a different athlete-growth MCP entry")
    servers[SERVER_NAME] = entry
    _save_config(path, document)
    return {"configured": True, "config_path": str(path), "server": SERVER_NAME}


def unconfigure_workbuddy(config_path: Path | None = None) -> dict[str, object]:
    path = config_path or Path.home() / ".workbuddy" / "mcp.json"
    if not path.exists():
        return {"configured": False, "server": SERVER_NAME}
    document = _read_config(path)
    servers = cast(dict[str, object], document.get("mcpServers", {}))
    if _looks_like_our_entry(servers.get(SERVER_NAME)):
        del servers[SERVER_NAME]
        _save_config(path, document)
    return {"configured": False, "server": SERVER_NAME}


def _looks_like_our_entry(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and entry.get("type") == "stdio"
        and isinstance(entry.get("args"), list)
        and entry["args"] == ["-m", "athlete_growth.interfaces.mcp.server"]
    )


def _read_config(path: Path) -> dict[str, object]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"mcpServers": {}}
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("WorkBuddy MCP config must be a regular file")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "WorkBuddy MCP config is not valid JSON; existing file was preserved"
        ) from exc
    if not isinstance(document, dict) or not isinstance(document.get("mcpServers", {}), dict):
        raise ValueError("WorkBuddy MCP config has an unsupported structure")
    return cast(dict[str, object], document)


def _save_config(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("WorkBuddy MCP config must be a regular file")
        backup = path.with_name("mcp.json.athlete-growth.bak")
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
            os.chmod(backup, 0o600)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".mcp-athlete-", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
