from __future__ import annotations

import json
import subprocess
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from athlete_growth.interfaces.cli.main import main
from athlete_growth.interfaces.host.codex_mcp_reload import reload_codex_mcp


class CodexMcpReloadTests(unittest.TestCase):
    def test_reload_uses_app_server_protocol_and_reports_scope(self) -> None:
        response = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"id":1,"result":{}}\n{"id":2,"result":{}}\n',
            stderr="",
        )
        with patch(
            "athlete_growth.interfaces.host.codex_mcp_reload.subprocess.run",
            return_value=response,
        ) as invoked:
            result = reload_codex_mcp(socket_path=Path("/private/tmp/test-codex.sock"))
        self.assertTrue(result["reload_requested"])
        self.assertFalse(result["current_desktop_task_verified"])
        command = invoked.call_args.args[0]
        self.assertEqual(command[:3], ["codex", "app-server", "proxy"])
        requests = [json.loads(line) for line in invoked.call_args.kwargs["input"].splitlines()]
        self.assertEqual(requests[2]["method"], "config/mcpServer/reload")
        self.assertIsNone(requests[2]["params"])

    def test_unreachable_socket_does_not_claim_reload(self) -> None:
        response = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="secret")
        with patch(
            "athlete_growth.interfaces.host.codex_mcp_reload.subprocess.run",
            return_value=response,
        ), self.assertRaisesRegex(RuntimeError, "control socket is unavailable") as raised:
            reload_codex_mcp()
        self.assertNotIn("secret", str(raised.exception))

    def test_cli_command_does_not_require_private_profile(self) -> None:
        with patch(
            "athlete_growth.interfaces.cli.commands.reload_codex_mcp",
            return_value={"reload_requested": True},
        ):
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["mcp-reload"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue()), {"reload_requested": True})


if __name__ == "__main__":
    unittest.main()
