from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from athlete_growth.adapters.storage import ProfileRegistry, SQLiteRecordStore, VaultLayout
from athlete_growth.domain.records import CanonicalRecord
from athlete_growth.interfaces.cli import main as cli_main
from athlete_growth.interfaces.mcp import server


class TrainingPreferenceTests(unittest.TestCase):
    def _profile(self, home: Path) -> tuple[str, SQLiteRecordStore]:
        with redirect_stdout(StringIO()):
            self.assertEqual(
                cli_main.main(
                    [
                        "init",
                        "--name",
                        "Synthetic Athlete",
                        "--timezone",
                        "UTC",
                        "--data-home",
                        str(home),
                    ]
                ),
                0,
            )
        athlete_id = ProfileRegistry(home / "config.toml").default_profile_id()
        assert athlete_id is not None
        return athlete_id, SQLiteRecordStore(VaultLayout(home, athlete_id).database_file)

    def _save_preference(self, home: Path, changes: dict[str, object]) -> dict[str, object]:
        preview = server.call_tool("preview_training_preferences", {"changes": changes}, home)
        self.assertFalse(preview["saved"])
        return server.call_tool(
            "confirm_training_preferences",
            {
                "changes": changes,
                "preview_hash": preview["preview_hash"],
            },
            home,
        )

    def test_confirmed_profile_preference_versions_are_private(self) -> None:
        with TemporaryDirectory() as temporary:
            home = Path(temporary) / "first"
            athlete_id, records = self._profile(home)
            other = Path(temporary) / "second"
            self._profile(other)
            changes = {"key_session_pattern": "focus_blocks", "aerobic_mixing": True}
            preview = server.call_tool("preview_training_preferences", {"changes": changes}, home)
            self.assertEqual(records.list_current(athlete_id, kind="training-preference"), [])
            with self.assertRaisesRegex(ValueError, "changed"):
                server.call_tool(
                    "confirm_training_preferences",
                    {
                        "changes": changes,
                        "preview_hash": preview["preview_hash"],
                    },
                    other,
                )
            first = server.call_tool(
                "confirm_training_preferences",
                {
                    "changes": changes,
                    "preview_hash": preview["preview_hash"],
                },
                home,
            )
            self.assertTrue(first["saved"])
            retried = server.call_tool(
                "confirm_training_preferences",
                {
                    "changes": changes,
                    "preview_hash": preview["preview_hash"],
                },
                home,
            )
            self.assertTrue(retried["already_confirmed"])
            self.assertEqual(retried["record_id"], first["record_id"])
            self.assertEqual(len(records.list_current(athlete_id, kind="training-preference")), 1)
            growth = server.call_tool("get_growth_profile", {}, home)
            self.assertEqual(growth["training_preferences"]["preferences"], changes)
            self.assertEqual(
                server.call_tool("get_training_preferences", {}, other)["preferences"], {}
            )
            second = self._save_preference(home, {"aerobic_mixing": False})
            self.assertEqual(second["supersedes_record_id"], first["record_id"])
            self.assertIsNotNone(records.get(str(first["record_id"])))
            self.assertEqual(len(records.list_current(athlete_id, kind="training-preference")), 1)
            stale = server.call_tool(
                "preview_training_preferences",
                {
                    "changes": {"focus_sport": "running"},
                },
                home,
            )
            self._save_preference(home, {"aerobic_mixing": None})
            with self.assertRaisesRegex(ValueError, "changed"):
                server.call_tool(
                    "confirm_training_preferences",
                    {
                        "changes": {"focus_sport": "running"},
                        "preview_hash": stale["preview_hash"],
                    },
                    home,
                )
            self.assertNotIn(
                "aerobic_mixing",
                server.call_tool("get_training_preferences", {}, home)["preferences"],
            )

    def test_plan_uses_confirmed_preferences_and_reviews_changes(self) -> None:
        with TemporaryDirectory() as temporary:
            home = Path(temporary) / "vault"
            athlete_id, records = self._profile(home)
            today = datetime.now(UTC)
            records.save(
                CanonicalRecord.create(
                    athlete_id=athlete_id,
                    kind="check-in",
                    occurred_at=today,
                    data={"pain": 0, "fatigue": 2, "soreness": 2, "illness": False},
                )
            )
            records.save(
                CanonicalRecord.create(
                    athlete_id=athlete_id,
                    kind="activity",
                    occurred_at=today - timedelta(days=1),
                    data={"sport": "cycling", "duration_min": 55},
                )
            )
            goal_draft = server.call_tool(
                "propose_goal",
                {
                    "idea": "Build cycling consistency",
                    "goal_type": "sport",
                },
                home,
            )
            goal = server.call_tool(
                "confirm_goal",
                {
                    "proposal_id": goal_draft["proposal_id"],
                    "revision": goal_draft["revision"],
                },
                home,
            )["goal"]
            saved = self._save_preference(
                home,
                {
                    "focus_sport": "cycling",
                    "aerobic_mixing": False,
                    "preferred_long_weekday": (today.weekday() + 3) % 7,
                },
            )
            availability = {str((today.weekday() + offset) % 7): 45 for offset in range(4)}
            args = {
                "goal_id": goal["goal_id"],
                "start_date": today.date().isoformat(),
                "available_minutes_by_weekday": availability,
                "max_weekly_minutes": 180,
                "sports": ["running", "cycling"],
            }
            preview = server.call_tool("preview_plan", args, home)
            self.assertEqual(
                preview["planning_context"]["preference_record_id"], saved["record_id"]
            )
            self.assertEqual(preview["planning_context"]["constraints"]["focus_sport"], "cycling")
            self.assertFalse(preview["planning_context"]["constraints"]["aerobic_mixing"])
            self.assertEqual({item["sport"] for item in preview["plan"]["sessions"]}, {"cycling"})
            confirmed = server.call_tool(
                "confirm_plan",
                {
                    "preview_id": preview["preview_id"],
                    "revision": preview["revision"],
                },
                home,
            )
            plan_id = confirmed["plan"]["plan_id"]
            self.assertFalse(
                server.call_tool("review_plan", {"plan_id": plan_id}, home)["review_required"]
            )
            self._save_preference(home, {"aerobic_mixing": True})
            review = server.call_tool("review_plan", {"plan_id": plan_id}, home)
            self.assertIn("confirmed training preferences changed", review["reasons"])
            updated = server.call_tool("preview_plan_update", {"plan_id": plan_id}, home)
            self.assertTrue(updated["planning_context"]["constraints"]["aerobic_mixing"])
            self.assertIsNone(
                updated["planning_context"]["requested_constraints"]["aerobic_mixing"]
            )
            explicit = server.call_tool(
                "preview_plan",
                {
                    **args,
                    "aerobic_mixing": False,
                },
                home,
            )
            self.assertFalse(explicit["planning_context"]["constraints"]["aerobic_mixing"])
            ignored = server.call_tool(
                "preview_plan",
                {
                    **args,
                    "use_saved_preferences": False,
                },
                home,
            )
            self.assertIsNone(ignored["planning_context"]["preference_record_id"])


if __name__ == "__main__":
    unittest.main()
