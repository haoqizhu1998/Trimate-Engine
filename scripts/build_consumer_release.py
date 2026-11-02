"""Build one host-neutral Trimate ZIP for AI conversations and local setup."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
import tomllib
import zipfile
from pathlib import Path

from athlete_growth.experience.bundle import build_bundle

ROOT = Path(__file__).resolve().parents[1]


def _product_version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project["version"]
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("expected a semantic project version")
    return version


def _release_version() -> str:
    version = (ROOT / "packaging" / "release-version.txt").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("expected a semantic release version")
    return version


def _zip_contents(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source))


def _validate_product_wheel(wheel: Path) -> None:
    """Reject accidental extra files before making a user-facing archive."""
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        has_experience = any(
            name.startswith("athlete_growth/experience/assets/") for name in names
        )
        if not names or not has_experience:
            raise ValueError("wheel is missing product experience assets")
        for name in names:
            parts = Path(name).parts
            if (
                name.startswith("/")
                or ".." in parts
                or not (
                    name.startswith("athlete_growth/")
                    or (len(parts) > 1 and parts[0].endswith(".dist-info"))
                )
            ):
                raise ValueError(f"unexpected wheel member: {name}")


def build_release(output: Path, wheel: Path | None = None) -> dict[str, Path]:
    product_version = _product_version()
    release_version = _release_version()
    tag = f"V{release_version}"
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="athlete-growth-release-") as tmp:
        stage = Path(tmp)
        if wheel is None:
            wheel_dir = stage / "wheel"
            wheel_dir.mkdir()
            subprocess.run(
                ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)],
                cwd=ROOT,
                check=True,
            )
            wheels = list(wheel_dir.glob("athlete_growth-*.whl"))
            if len(wheels) != 1:
                raise RuntimeError("expected one product wheel")
            wheel = wheels[0]
        wheel = wheel.resolve()
        if (
            not wheel.name.startswith(f"athlete_growth-{product_version}-")
            or wheel.suffix != ".whl"
        ):
            raise ValueError(f"expected an athlete-growth {product_version} wheel")
        _validate_product_wheel(wheel)

        generic = build_bundle(stage / "agent-bundle") / "generic"

        consumer = stage / f"Trimate-{tag}-macOS-安装包"
        consumer.mkdir()
        for name in ("LICENSE", "NOTICE"):
            shutil.copy2(ROOT / name, consumer / name)
        shutil.copy2(wheel, consumer / wheel.name)
        shutil.copy2(generic / "agent.md", consumer / "Trimate常驻指令.md")
        shutil.copy2(ROOT / "packaging" / "common" / "bootstrap-local.py", consumer)
        for name in ("安装Trimate.command", "开始使用.command", "卸载Trimate.command"):
            target = consumer / name
            shutil.copy2(ROOT / "packaging" / "macos" / name, target)
            target.chmod(0o755)
        (consumer / "使用说明.md").write_text(
            "# Trimate Mac 安装包\n\n"
            "1. 解压后双击 `安装Trimate.command`，安装和个人档案引导会自动连续运行；"
            "首次安装需要网络。\n"
            "2. 回到当前 AI 对话，让 AI 检查本机 Trimate 和 MCP 连接状态；"
            "若当前宿主尚未配置连接器，由 AI 按该宿主的设置和授权流程继续。\n\n"
            "佳明、高驰属于产品通用数据来源，由每位用户自行授权。"
            "高驰的本机同步为试验能力：使用官方 OAuth helper，首次试同步成功后才开启自动轮询。"
            "官方 MCP 若不返回机器可读活动 ID，本机同步会明确停止。\n\n"
            "个人数据默认保存在 `~/.athlete-growth`，不会出现在此安装包。"
            "卸载时双击 `卸载Trimate.command`；个人数据会保留。\n",
            encoding="utf-8",
        )
        shutil.copy2(ROOT / "packaging" / "connectors" / "coros-cn-mcp.json", consumer)
        windows = stage / f"Trimate-{tag}-Windows-安装包"
        windows.mkdir()
        for name in ("LICENSE", "NOTICE"):
            shutil.copy2(ROOT / name, windows / name)
        shutil.copy2(wheel, windows / wheel.name)
        shutil.copy2(generic / "agent.md", windows / "Trimate常驻指令.md")
        shutil.copy2(ROOT / "packaging" / "common" / "bootstrap-local.py", windows)
        for name in (
            "Install-Windows.cmd", "Install-Windows.ps1", "Start-Windows.cmd",
            "Start-Windows.ps1", "Uninstall-Windows.cmd", "Uninstall-Windows.ps1",
        ):
            source = ROOT / "packaging" / "windows" / name
            target = windows / name
            if name.endswith(".ps1"):
                target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8-sig")
            else:
                content = source.read_bytes().replace(b"\r\n", b"\n")
                target.write_bytes(content.replace(b"\n", b"\r\n"))
        (windows / "使用说明.md").write_text(
            "# Trimate Windows 安装包\n\n"
            "解压后双击 `Install-Windows.cmd`。安装程序会配置 Python 环境、"
            "个人档案和可选设备同步；首次安装需要网络。"
            "完成后回到当前 AI 对话检查本机 MCP 连接。个人数据保存在用户目录的"
            "`.athlete-growth`，不会随安装包分享。\n",
            encoding="utf-8",
        )
        # The conversation package is a single attachment with a readable entry
        # point. It retains local installers for hosts that can execute tasks,
        # while web-only hosts can use the same Agent/Skill material in chat.
        ai = stage / "ai-conversation"
        ai.mkdir()
        for name in ("LICENSE", "NOTICE"):
            shutil.copy2(ROOT / name, ai / name)
        for name in ("00-请AI先读.md", "00-给用户看.md"):
            shutil.copy2(ROOT / "packaging" / "common" / name, ai / name)
        shutil.copy2(ROOT / "packaging" / "common" / "宿主连接.md", ai / "宿主连接.md")
        shutil.copytree(generic, ai / "product")
        shutil.copy2(ROOT / "experience" / "manifest.json", ai / "product" / "manifest.json")
        shutil.copytree(consumer, ai / "install" / "macOS")
        shutil.copytree(windows, ai / "install" / "Windows")
        (ai / "release.json").write_text(
            json.dumps(
                {
                    "product": "Trimate",
                    "release_version": release_version,
                    "engine_version": product_version,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        ai_zip = output / f"Trimate-{tag}.zip"
        with tempfile.NamedTemporaryFile(
            prefix=".trimate-release-", suffix=".zip", dir=output, delete=False
        ) as handle:
            staged_zip = Path(handle.name)
        try:
            _zip_contents(ai, staged_zip)
            with zipfile.ZipFile(staged_zip) as archive:
                if archive.testzip() is not None:
                    raise RuntimeError("consumer ZIP failed integrity verification")
            staged_zip.replace(ai_zip)
        finally:
            staged_zip.unlink(missing_ok=True)
        for older in output.iterdir():
            if (
                older != ai_zip
                and older.is_file()
                and re.fullmatch(r"Trimate-V\d+\.\d+\.\d+\.zip", older.name)
            ):
                older.unlink()
    return {"ai_conversation": ai_zip}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "consumer")
    parser.add_argument("--wheel", type=Path)
    args = parser.parse_args()
    artifacts = build_release(args.output, args.wheel)
    for name, path in artifacts.items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
