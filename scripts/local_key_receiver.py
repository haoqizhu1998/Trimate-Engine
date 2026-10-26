#!/usr/bin/env python3
"""Receive a secret from a loopback-only form and store it owner-only."""

from __future__ import annotations

import html
import os
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
KEY_FILE = PROJECT_ROOT / ".secrets" / "garmin-global-personal-api-key"
NONCE = secrets.token_urlsafe(24)


class Handler(BaseHTTPRequestHandler):
    server_version = "GarminKeyReceiver/1.0"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        if query.get("token") != [NONCE]:
            self.send_error(404)
            return
        page = f"""<!doctype html>
<meta charset="utf-8">
<title>保存 Garmin API Key</title>
<style>
body {{ font: 16px system-ui; max-width: 620px; margin: 80px auto; padding: 0 24px; }}
input, button {{ font: inherit; padding: 12px; }}
input {{ width: 100%; box-sizing: border-box; }}
button {{ margin-top: 16px; }}
</style>
<h1>保存 Garmin Personal API Key</h1>
<p>此页面仅连接本机 127.0.0.1。密钥将保存到当前项目的受保护目录。</p>
<form method="post" action="/save">
  <input type="hidden" name="token" value="{html.escape(NONCE)}">
  <label>API Key<input name="key" type="password" autocomplete="off" required autofocus></label>
  <button type="submit">安全保存</button>
</form>"""
        body = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/save":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 4096:
            self.send_error(400)
            return
        fields = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        key = fields.get("key", [""])[0].strip()
        if fields.get("token") != [NONCE] or not key:
            self.send_error(400)
            return

        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(KEY_FILE.parent, 0o700)
        descriptor = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(key + "\n")
        os.chmod(KEY_FILE, 0o600)

        body = "密钥已安全保存。可以关闭此页面。".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        self.server.saved = True  # type: ignore[attr-defined]


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.saved = False  # type: ignore[attr-defined]
    port = server.server_address[1]
    print(f"http://127.0.0.1:{port}/?token={NONCE}", flush=True)
    while not server.saved:  # type: ignore[attr-defined]
        server.handle_request()
    server.server_close()
    print("SAVED", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
