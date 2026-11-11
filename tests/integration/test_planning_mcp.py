from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from athlete_growth.adapters.storage import ProfileRegistry, SQLiteRecordStore, VaultLayout
from athlete_growth.domain.records import CanonicalRecord
from athlete_growth.interfaces.cli import main as cli_main
from athlete_growth.interfaces.mcp import server


class PlanningMCPTests(unittest.TestCase):
    def test_private_mcp_draft_confirm_round_trip(self) -> None:
        with TemporaryDirectory() as temporary:
            home = Path(temporary) / "vault"
            with redirect_stdout(StringIO()):
                exit_code = cli_main.main(
                    [
                        "init",
                        "--name",
                        "Test Athlete",
                        "--timezone",
                        "UTC",
                        "--data-home",
                        str(home),
                    ]
                )
            self.assertEqual(exit_code, 0)
            athlete_id = ProfileRegistry(home / "config.toml").default_profile_id()
            assert athlete_id is not None
            records = SQLiteRecordStore(VaultLayout(home, athlete_id).database_file)

            proposal = server.call_tool(
                "propose_goal",
                {"idea": "我想完成第一次 10 公里比赛", "goal_type": "race"},
                home,
            )
            self.assertEqual(records.list_current(athlete_id, kind="goal"), [])
            self.assertEqual(
                len(server.call_tool("list_open_planning_drafts", {}, home)["drafts"]), 1
            )
            goal_result = server.call_tool(
                "confirm_goal",
                {"proposal_id": proposal["proposal_id"], "revision": proposal["revision"]},
                home,
            )
            goal = goal_result["goal"]
            assert isinstance(goal, dict)
            self.assertEqual(len(records.list_current(athlete_id, kind="goal")), 1)

            today = datetime.now(UTC).date()
            preview = server.call_tool(
                "preview_plan",
                {
                    "goal_id": goal["goal_id"],
                    "start_date": today.isoformat(),
                    "available_minutes_by_weekday": {str(today.weekday()): 45},
                    "max_weekly_minutes": 45,
                    "sport": "running",
                },
                home,
            )
            self.assertFalse(preview["blocked"])
            self.assertEqual(records.list_current(athlete_id, kind="training-plan"), [])
            confirmed = server.call_tool(
                "confirm_plan",
                {"preview_id": preview["preview_id"], "revision": preview["revision"]},
                home,
            )
            self.assertTrue(confirmed["confirmed"])
            self.assertEqual(len(records.list_current(athlete_id, kind="training-plan")), 1)
            self.assertEqual(server.call_tool("list_open_planning_drafts", {}, home)["drafts"], [])
            multisport = server.call_tool(
                "preview_plan",
                {
                    "goal_id": goal["goal_id"],
                    "start_date": today.isoformat(),
                    "available_minutes_by_weekday": {
                        str((today.weekday() + offset) % 7): 50 for offset in range(3)
                    },
                    "max_weekly_minutes": 150,
                    "sports": ["swimming", "cycling", "running"],
                },
                home,
            )
            self.assertEqual(
                {item["sport"] for item in multisport["plan"]["sessions"]},
                {"swimming", "cycling", "running"},
            )
            self.assertEqual(multisport["unscheduled_sports"], [])
            self.assertIn("seven-day evidence-bounded", multisport["plan_scope"])
            structured = server.call_tool(
                "preview_plan",
                {
                    "goal_id": goal["goal_id"],
                    "start_date": today.isoformat(),
                    "available_minutes_by_weekday": {str(today.weekday()): 90},
                    "max_weekly_minutes": 90,
                    "sports": ["cycling", "running"],
                    "requested_sessions": [
                        {"session_date": today.isoformat(), "sport": "cycling",
                         "duration_min": 60, "distance_m": 25000,
                         "session_type": "brick", "brick_id": "transition-1"},
                        {"session_date": today.isoformat(), "sport": "running",
                         "duration_min": 20, "distance_m": 3000,
                         "session_type": "brick", "brick_id": "transition-1"},
                    ],
                },
                home,
            )
            self.assertEqual(structured["summary"]["per_sport_distance_m"], {
                "cycling": 25000, "running": 3000,
            })
            self.assertEqual(structured["summary"]["two_session_days"], [today.isoformat()])
            self.assertEqual(
                [item["distance_m"] for item in structured["plan"]["sessions"]],
                [25000, 3000],
            )
            distance_only = server.call_tool(
                "preview_plan",
                {
                    "goal_id": goal["goal_id"],
                    "start_date": today.isoformat(),
                    "sport": "running",
                    "requested_sessions": [
                        {"session_date": today.isoformat(), "sport": "running",
                         "distance_m": 10000},
                    ],
                },
                home,
            )
            self.assertIsNone(distance_only["plan"]["sessions"][0]["duration_min"])
            self.assertIsNone(distance_only["summary"]["total_minutes"])
            self.assertEqual(distance_only["summary"]["per_sport_distance_m"], {
                "running": 10000,
            })
            reviewed = server.call_tool(
                "review_plan", {"plan_id": confirmed["plan"]["plan_id"]}, home
            )
            self.assertFalse(reviewed["review_required"])
            updated = server.call_tool(
                "preview_plan_update", {"plan_id": confirmed["plan"]["plan_id"]}, home
            )
            self.assertEqual(updated["revision"], 2)
            self.assertIn("replanning_context", updated)
            session_id = confirmed["plan"]["sessions"][0]["session_id"]
            adjustment = server.call_tool(
                "preview_plan_adjustment",
                {
                    "plan_id": confirmed["plan"]["plan_id"],
                    "session_id": session_id,
                    "action": "skip",
                    "reason": "Schedule conflict",
                },
                home,
            )
            self.assertEqual(adjustment["adjustment_context"]["action"], "skip")
            self.assertEqual(len(records.list_current(athlete_id, kind="training-plan")), 1)
            activity = CanonicalRecord.create(
                athlete_id=athlete_id, kind="activity", occurred_at=datetime.now(UTC),
                data={"sport": "running", "duration_min": 40},
            )
            records.save(activity)
            plan_id = str(confirmed["plan"]["plan_id"])
            pending = server.call_tool("list_plan_execution", {"plan_id": plan_id}, home)
            self.assertEqual(pending["pending_reviews"][0]["match_state"], "suggested")
            self.assertEqual(
                pending["pending_reviews"][0]["activity_candidates"][0]["activity_id"],
                activity.record_id,
            )
            feedback = {
                "plan_id": plan_id, "session_id": session_id, "status": "completed",
                "activity_id": activity.record_id, "rpe": 5, "pain_after": 0,
            }
            review_feedback = server.call_tool("preview_plan_execution", feedback, home)
            self.assertEqual(server.call_tool(
                "list_plan_execution", {"plan_id": plan_id}, home
            )["count"], 0)
            server.call_tool("confirm_plan_execution", {
                **feedback, "preview_hash": review_feedback["preview_hash"],
            }, home)
            self.assertEqual(server.call_tool(
                "list_plan_execution", {"plan_id": plan_id}, home
            )["pending_reviews"], [])
            removal = server.call_tool(
                "preview_goal_change",
                {"action": "delete", "goal_id": goal["goal_id"], "reason": "goal withdrawn"},
                home,
            )
            self.assertEqual(removal["action"], "delete")
            server.call_tool(
                "confirm_goal_change",
                {"preview_id": removal["preview_id"], "revision": removal["revision"]},
                home,
            )
            self.assertTrue(
                server.call_tool("review_plan", {"plan_id": confirmed["plan"]["plan_id"]}, home)[
                    "review_required"
                ]
            )


if __name__ == "__main__":
    unittest.main()
