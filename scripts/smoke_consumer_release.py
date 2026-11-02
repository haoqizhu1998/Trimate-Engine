"""Exercise a packaged macOS release in an isolated temporary user home.

This is an opt-in release smoke test. It can download package dependencies, but
never reads or modifies the caller's Trimate profile or MCP configuration.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path


def _run(command: list[str], *, env: dict[str, str], timeout: int = 180) -> str:
    completed = subprocess.run(
        command, env=env, text=True, capture_output=True, timeout=timeout, check=False
    )
    if completed.returncode:
        raise RuntimeError(
            f"release smoke command failed ({completed.returncode}): "
            + (completed.stderr or completed.stdout)[-1200:]
        )
    return completed.stdout


def smoke(archive_path: Path) -> dict[str, object]:
    if sys.platform != "darwin":
        raise RuntimeError("macOS release smoke requires macOS")
    with tempfile.TemporaryDirectory(prefix="trimate-release-smoke-") as temporary:
        root = Path(temporary)
        bundle = root / "bundle"
        bundle.mkdir()
        home = root / "home"
        home.mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.namelist():
                path = Path(member)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("release archive contains an unsafe path")
            archive.extractall(bundle)
        release = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
        if release.get("product") != "Trimate":
            raise ValueError("release identity is invalid")
        installer = bundle / "install" / "macOS" / "安装Trimate.command"
        if not installer.is_file():
            raise ValueError("macOS installer is missing")

        env = dict(os.environ)
        env.update({
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "UV_CACHE_DIR": str(root / "uv-cache"),
            "UV_PYTHON_DOWNLOADS": "never",
        })
        output = _run(["bash", str(installer), "--automated", "codex"], env=env)
        result = json.loads(output.splitlines()[-1])
        if not result.get("ok") or not result.get("profile_ready"):
            raise RuntimeError("release did not initialize an isolated profile")
        config = tomllib.loads((home / ".codex" / "config.toml").read_text())
        entry = config["mcp_servers"]["athlete_growth"]
        python = Path(entry["command"])
        if not python.is_file() or home not in python.parents:
            raise RuntimeError("Codex MCP does not point to the isolated installation")
        cli = home / ".local" / "share" / "athlete-growth" / "venv" / "bin" / "athlete-growth"
        bootstrap = bundle / "install" / "macOS" / "bootstrap-local.py"
        repeated = json.loads(_run([
            str(python), str(bootstrap), "--host", "codex", "--cli", str(cli),
        ], env=env).splitlines()[-1])
        if repeated.get("profile_created") or repeated.get("mcp_config") != "already_configured":
            raise RuntimeError("release setup is not idempotent")
        workbuddy = json.loads(_run([
            str(python), str(bootstrap), "--host", "workbuddy", "--cli", str(cli),
        ], env=env).splitlines()[-1])
        if not workbuddy.get("ok"):
            raise RuntimeError("WorkBuddy local configuration failed")
        workbuddy_config = json.loads((home / ".workbuddy" / "mcp.json").read_text())
        if "athlete-growth" not in workbuddy_config.get("mcpServers", {}):
            raise RuntimeError("WorkBuddy MCP entry is missing")

        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "get_onboarding_status", "arguments": {},
            }},
        ]
        mcp_env = {**env, **entry.get("env", {})}
        completed = subprocess.run(
            [str(python), *entry["args"]],
            input="".join(json.dumps(item) + "\n" for item in requests),
            text=True, capture_output=True, env=mcp_env, timeout=30, check=False,
        )
        if completed.returncode:
            raise RuntimeError("packaged MCP failed to start: " + completed.stderr[-1000:])
        responses = {item["id"]: item for line in completed.stdout.splitlines()
                     if (item := json.loads(line)).get("id") is not None}
        tools = responses[2]["result"]["tools"]
        names = {item["name"] for item in tools}
        if "list_plan_execution" not in names or "get_onboarding_status" not in names:
            raise RuntimeError("packaged MCP tool list is incomplete")
        onboarding = responses[3]["result"]
        if onboarding.get("isError"):
            raise RuntimeError("packaged MCP onboarding call failed")
        return {
            "release_version": release["release_version"],
            "profile_created": True,
            "codex_configured": True,
            "workbuddy_configured": True,
            "mcp_started": True,
            "tool_count": len(names),
            "onboarding_readable": True,
            "real_workbuddy_client_verified": False,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    print(json.dumps(smoke(args.archive.expanduser().resolve()), ensure_ascii=False))


if __name__ == "__main__":
    main()
