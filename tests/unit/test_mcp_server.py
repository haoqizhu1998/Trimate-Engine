from __future__ import annotations

import json
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from athlete_growth.interfaces.host.catalog import capability_catalog
from athlete_growth.interfaces.mcp import server


class MCPServerTests(unittest.TestCase):
    home = Path("/private/test-vault")

    def test_initialize_and_catalog_marks_planning_as_writes(self) -> None:
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
        declared = {
            name
            for capability in capability_catalog()["capabilities"]
            for name in capability["tools"]
        }
        self.assertEqual(names, declared | {"get_capability_catalog"})
        by_name = {tool["name"]: tool for tool in listed["result"]["tools"]}
        self.assertTrue(by_name["list_goals"]["annotations"]["readOnlyHint"])
        self.assertTrue(by_name["list_open_planning_drafts"]["annotations"]["readOnlyHint"])
        self.assertFalse(by_name["confirm_plan"]["annotations"]["readOnlyHint"])
        self.assertFalse(by_name["confirm_check_in"]["annotations"]["readOnlyHint"])
        self.assertFalse(by_name["run_sync_now"]["annotations"]["readOnlyHint"])
        self.assertIn("explicit approval", initialized["result"]["instructions"])

    def test_dispatch_reuses_application_and_blocks_extra_arguments(self) -> None:
        with patch.object(
            server.host_capabilities, "daily_report", return_value={"period": "2026-10-27"}
        ) as daily:
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
        self.assertEqual(daily.call_args.args[0], self.home)
        self.assertEqual(daily.call_args.args[1].isoformat(), "2026-10-27")

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
            server.host_tools,
            "automation_status",
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

    def test_planning_tool_dispatch_and_argument_validation(self) -> None:
        with patch.object(
            server.planning_tools,
            "propose_goal",
            return_value={"proposal_id": "draft-1", "requires_confirmation": True},
        ) as propose:
            response = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {
                        "name": "propose_goal",
                        "arguments": {"idea": "我想跑完半马", "goal_type": "race"},
                    },
                },
                self.home,
            )
        assert response is not None
        self.assertEqual(response["result"]["structuredContent"]["proposal_id"], "draft-1")
        self.assertEqual(propose.call_args.kwargs["goal_type"], "race")

        rejected = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "preview_plan",
                    "arguments": {
                        "goal_id": "g1",
                        "start_date": "2026-10-27",
                        "available_minutes_by_weekday": {"9": 45},
                        "max_weekly_minutes": 45,
                        "sport": "running",
                    },
                },
            },
            self.home,
        )
        assert rejected is not None
        self.assertTrue(rejected["result"]["isError"])

    def test_planning_errors_are_actionable_without_leaking_paths(self) -> None:
        with patch.object(
            server.planning_tools,
            "confirm_plan",
            side_effect=ValueError("source data changed; preview the plan again"),
        ):
            response = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "tools/call",
                    "params": {
                        "name": "confirm_plan",
                        "arguments": {"preview_id": "p1", "revision": 1},
                    },
                },
                self.home,
            )
        assert response is not None
        self.assertEqual(
            response["result"]["structuredContent"]["error"],
            "source data changed; preview the plan again",
        )

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
