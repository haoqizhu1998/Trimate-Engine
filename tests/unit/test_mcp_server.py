from __future__ import annotations

import json
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from athlete_growth.interfaces.mcp import server


class MCPServerTests(unittest.TestCase):
    home = Path("/private/test-vault")

    def test_initialize_and_read_only_catalog(self) -> None:
        initialized = server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, self.home
        )
        assert initialized is not None
        self.assertEqual(initialized["result"]["protocolVersion"], server.PROTOCOL_VERSION)

        listed = server.handle_message(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, self.home
        )
        assert listed is not None
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertEqual(
            names,
            {
                "get_sync_status",
                "get_today_report",
                "get_weekly_report",
                "list_goals",
                "list_notifications",
            },
        )
        self.assertTrue(
            all(tool["annotations"]["readOnlyHint"] for tool in listed["result"]["tools"])
        )

    def test_dispatch_reuses_application_and_blocks_extra_arguments(self) -> None:
        with patch.object(
            server.commands, "execute", return_value={"period": "2026-10-27"}
        ) as execute:
            response = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "get_today_report", "arguments": {"date": "2026-10-27"}},
                },
                self.home,
            )
        assert response is not None
        self.assertEqual(response["result"]["structuredContent"], {"period": "2026-10-27"})
        parsed = execute.call_args.args[0]
        self.assertEqual(parsed.command, "today")
        self.assertEqual(parsed.date.isoformat(), "2026-10-27")

        rejected = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "get_today_report", "arguments": {"data_home": "/tmp/other"}},
            },
            self.home,
        )
        assert rejected is not None
        self.assertTrue(rejected["result"]["isError"])

    def test_tool_failures_do_not_leak_paths_or_credentials(self) -> None:
        with patch.object(
            server.commands,
            "execute",
            side_effect=RuntimeError("secret at /private/test-vault/token"),
        ):
            response = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {"name": "get_sync_status", "arguments": {}},
                },
                self.home,
            )
        assert response is not None
        encoded = json.dumps(response)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("test-vault", encoded)

    def test_stdio_one_json_response_per_request_not_notification(self) -> None:
        reader = StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
            + "\n"
        )
        writer = StringIO()
        server.serve(reader, writer, self.home)
        lines = writer.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["result"], {})


if __name__ == "__main__":
    unittest.main()
