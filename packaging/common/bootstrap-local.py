"""Finish a Trimate install for a local AI host without interactive prompts."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=False)


def _local_timezone() -> str:
    configured = os.environ.get("TZ", "").strip()
    if configured and "/" in configured:
        return configured
    if platform.system() == "Windows":
        windows_name = _run("tzutil", "/g")
        if windows_name.returncode == 0:
            # China is the first supported consumer market. Other zones can be
            # corrected during onboarding instead of silently using UTC.
            aliases = {"China Standard Time": "Asia/Shanghai", "UTC": "Etc/UTC"}
            return aliases.get(windows_name.stdout.strip(), "Asia/Shanghai")
        return "Asia/Shanghai"
    localtime = Path("/etc/localtime")
    try:
        return str(localtime.resolve()).split("/zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return "Asia/Shanghai"


def ensure_profile(cli: Path, data_home: Path) -> bool:
    status = _run(str(cli), "status", "--data-home", str(data_home))
    if status.returncode == 0:
        return False
    # Only a missing profile can be initialized. Never replace an existing vault.
    if data_home.exists() and any(data_home.iterdir()):
        raise RuntimeError("本地数据目录已有内容，但档案无法读取；已保留原数据，请检查后重试")
    timezone = _local_timezone()
    created = _run(
        str(cli), "init", "--name", "运动员", "--timezone", timezone,
        "--units", "metric", "--data-home", str(data_home),
    )
    if created.returncode:
        raise RuntimeError("建立本地档案失败：" + created.stderr.strip()[-500:])
    return True


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def configure_codex(python: Path, data_home: Path, path: Path) -> str:
    if path.exists():
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("Codex 配置不是普通文件，已保留原配置")
        original = path.read_bytes()
        try:
            document = tomllib.loads(original.decode("utf-8"))
        except (UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise RuntimeError("Codex 配置无法解析，已保留原配置") from exc
    else:
        original = b""
        document = {}
    servers = document.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise RuntimeError("Codex MCP 配置结构不受支持，已保留原配置")
    expected = {
        "command": str(python),
        "args": ["-m", "athlete_growth.interfaces.mcp.server"],
        "env": {"ATHLETE_GROWTH_HOME": str(data_home)},
    }
    previous = servers.get("athlete_growth")
    if previous == expected:
        return "already_configured"
    if previous is not None:
        raise RuntimeError("Codex 已有 athlete_growth MCP 条目，已保留原配置")
    stanza = (
        "\n[mcp_servers.athlete_growth]\n"
        f"command = {_toml_string(str(python))}\n"
        'args = ["-m", "athlete_growth.interfaces.mcp.server"]\n'
        "[mcp_servers.athlete_growth.env]\n"
        f"ATHLETE_GROWTH_HOME = {_toml_string(str(data_home))}\n"
    )
    content = (
        original
        + (b"\n" if original and not original.endswith(b"\n") else b"")
        + stanza.encode()
    )
    tomllib.loads(content.decode("utf-8"))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if original:
        backup = path.with_name(path.name + ".trimate.bak")
        if not backup.exists():
            backup.write_bytes(original)
            os.chmod(backup, 0o600)
    descriptor, temp_name = tempfile.mkstemp(prefix=".trimate-", dir=path.parent)
    temporary = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return "configured"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("codex", "workbuddy"), required=True)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--data-home", type=Path, default=Path.home() / ".athlete-growth")
    args = parser.parse_args()
    cli = args.cli.expanduser().resolve()
    home = args.data_home.expanduser().resolve()
    if not cli.is_file():
        parser.error("本地 Trimate CLI 不存在；先运行同包的系统安装脚本")
    try:
        created = ensure_profile(cli, home)
        if args.host == "workbuddy":
            result = _run(str(cli), "workbuddy-configure", "--data-home", str(home))
            if result.returncode:
                raise RuntimeError("WorkBuddy MCP 配置失败：" + result.stderr.strip()[-500:])
            config_state = "configured"
        else:
            config_root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
            config_state = configure_codex(
                Path(sys.executable).absolute(), home, config_root / "config.toml"
            )
    except (OSError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({
        "ok": True, "host": args.host, "profile_created": created,
        "profile_ready": True, "mcp_config": config_state,
        "tool_verified": False, "host_reload_required": True,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
