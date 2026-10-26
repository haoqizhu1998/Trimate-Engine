#!/usr/bin/env python3
"""Store a Garmin Personal API key locally without echoing it."""

from __future__ import annotations

import getpass
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
KEY_FILE = PROJECT_ROOT / ".secrets" / "garmin-global-personal-api-key"


def main() -> int:
    key = getpass.getpass("请粘贴 Garmin Personal API Key（输入不会显示）：").strip()
    if not key:
        print("未输入密钥，未保存。")
        return 1

    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(KEY_FILE.parent, 0o700)
    descriptor = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(key + "\n")
    os.chmod(KEY_FILE, 0o600)

    print(f"密钥已安全保存：{KEY_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
