from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from athlete_growth.adapters.storage import ProfileRegistry, SQLiteRecordStore, VaultLayout
from athlete_growth.adapters.storage.sqlite_growth import SQLiteGrowthRepository
from athlete_growth.application.learn.decision import GrowthDecisionService
from athlete_growth.domain.records import CanonicalRecord
from athlete_growth.interfaces.cli import main as cli_main
from athlete_growth.interfaces.mcp import server


class GrowthDecisionLoopTests(unittest.TestCase):
    def _init(self, home: Path) -> tuple[str, SQLiteRecordStore, SQLiteGrowthRepository]:
        with redirect_stdout(StringIO()):
            result = cli_main.main(
                [
                    "init", "--name", "Synthetic Athlete", "--timezone", "UTC",
                    "--data-home", str(home),
                ]
            )
        self.assertEqual(result, 0)
        athlete_id = ProfileRegistry(home / "config.toml").default_profile_id()
        assert athlete_id is not None
        records = SQLiteRecordStore(VaultLayout(home, athlete_id).database_file)
        return athlete_id, records, SQLiteGrowthRepository(records.database_file, records)

    @staticmethod
    def _check_in(records: SQLiteRecordStore, athlete_id: str, on: datetime, pain: int) -> None:
        records.save(
            CanonicalRecord.create(
                athlete_id=athlete_id,
                kind="check-in",
                occurred_at=on,
                data={"fatigue": 2, "soreness": 1, "pain": pain, "illness": False},
            )
        )

    def test_preview_confirm_is_evidence_bound_and_safety_overrides(self) -> None:
        with TemporaryDirectory() as temporary:
            athlete_id, records, growth = self._init(Path(temporary) / "vault")
            now = datetime(2026, 10, 27, 8, tzinfo=UTC)
            clock_value = [now]
            service = GrowthDecisionService(
                athlete_id, records, growth, timezone_name="UTC", clock=lambda: clock_value[0]
            )
            initial = service.preview_decision()
            self.assertEqual(initial["risk_level"], "unknown")
            self.assertEqual([item["id"] for item in initial["options"]], ["easy", "pause"])
            self.assertFalse(initial["saved"])
            with self.assertRaisesRegex(ValueError, "not allowed"):
                service.confirm_decision(str(initial["preview_hash"]), "usual")

            self._check_in(records, athlete_id, now, pain=0)
            with self.assertRaisesRegex(ValueError, "context changed"):
                service.confirm_decision(str(initial["preview_hash"]), "easy")
            low = service.preview_decision()
            self.assertEqual(low["risk_level"], "low")
            self.assertEqual(low["recommended_option"], "easy")
            self.assertEqual(low["recommendation_basis"], "limited data")
            chosen = service.confirm_decision(str(low["preview_hash"]), "usual")
            self.assertEqual(chosen["selected_option"], "usual")
            self.assertEqual(
                service.confirm_decision(str(low["preview_hash"]), "usual")["decision_id"],
                chosen["decision_id"],
            )

            later = now + timedelta(hours=1)
            self._check_in(records, athlete_id, later, pain=8)
            clock_value[0] = later
            high = service.preview_decision()
            self.assertEqual(high["risk_level"], "high")
            self.assertEqual([item["id"] for item in high["options"]], ["pause"])
            with self.assertRaisesRegex(ValueError, "not allowed"):
                service.confirm_decision(str(high["preview_hash"]), "usual")
            paused = service.confirm_decision(str(high["preview_hash"]), "pause")
            self.assertEqual(paused["supersedes_id"], chosen["decision_id"])
            with self.assertRaisesRegex(ValueError, "superseded"):
                service.confirm_decision(str(low["preview_hash"]), "usual")
            with self.assertRaisesRegex(ValueError, "superseded"):
                service.record_outcome(
                    decision_id=str(chosen["decision_id"]),
                    expected_revision=0,
                    completed=True,
                    effect="same",
                )

    def test_hrv_trend_is_context_and_does_not_override_safe_options(self) -> None:
        with TemporaryDirectory() as temporary:
            athlete_id, records, growth = self._init(Path(temporary) / "vault")
            now = datetime(2026, 10, 27, 8, tzinfo=UTC)
            for days_ago in range(28):
                night = now - timedelta(days=days_ago)
                records.save(CanonicalRecord.create(
                    athlete_id=athlete_id, kind="hrv", occurred_at=night,
                    data={"calendar_date": night.date().isoformat(),
                          "last_night_avg_ms": 40 if days_ago < 7 else 60},
                ))
            self._check_in(records, athlete_id, now, pain=0)
            service = GrowthDecisionService(
                athlete_id, records, growth, timezone_name="UTC", clock=lambda: now
            )
            preview = service.preview_decision()

        self.assertEqual(preview["risk_level"], "low")
        self.assertEqual(preview["hrv_context"]["recent_vs_prior_pct"], -33.3)
        self.assertIn("usual", [item["id"] for item in preview["options"]])

    def test_repeated_feedback_requires_user_approval_before_adaptation(self) -> None:
        with TemporaryDirectory() as temporary:
            athlete_id, records, growth = self._init(Path(temporary) / "vault")
            start = datetime(2026, 10, 1, 8, tzinfo=UTC)
            clock_value = [start]
            service = GrowthDecisionService(
                athlete_id, records, growth, timezone_name="UTC", clock=lambda: clock_value[0]
            )
            for day in range(6):
                clock_value[0] = start + timedelta(days=day)
                self._check_in(records, athlete_id, clock_value[0], pain=0)
                preview = service.preview_decision()
                choice = "usual" if day < 3 else "easy"
                decision = service.confirm_decision(str(preview["preview_hash"]), choice)
                result = service.record_outcome(
                    decision_id=str(decision["decision_id"]),
                    expected_revision=0,
                    completed=True,
                    effect="worse" if choice == "usual" else "better",
                    pain_after=2 if choice == "usual" else 0,
                )
                self.assertEqual(result["feedback"]["revision"], 1)
                if day < 5:
                    self.assertIsNone(result["growth_profile"]["candidate_adaptation"])
            profile = service.growth_profile()
            candidate = profile["candidate_adaptation"]
            assert isinstance(candidate, dict)
            self.assertEqual(candidate["preferred_option"], "easy")
            self.assertFalse(profile["plan_changed"])
            self.assertIn("user reports only", str(candidate["limitations"]))
            self._check_in(
                records, athlete_id, clock_value[0] + timedelta(hours=1), pain=0
            )
            with self.assertRaisesRegex(ValueError, "adaptation changed"):
                service.confirm_adaptation(str(candidate["adaptation_id"]))
            candidate = service.growth_profile()["candidate_adaptation"]
            assert isinstance(candidate, dict)
            accepted = service.confirm_adaptation(str(candidate["adaptation_id"]))
            self.assertEqual(accepted["active_adaptation"]["preferred_option"], "easy")

            clock_value[0] = start + timedelta(days=6)
            self._check_in(records, athlete_id, clock_value[0], pain=0)
            adapted = service.preview_decision()
            self.assertEqual(adapted["recommended_option"], "easy")
            self.assertEqual(adapted["recommendation_basis"], "user-approved adaptation")
            clock_value[0] = start + timedelta(days=7)
            self._check_in(records, athlete_id, clock_value[0], pain=8)
            high = service.preview_decision()
            self.assertEqual(high["recommended_option"], "pause")
            self.assertEqual([item["id"] for item in high["options"]], ["pause"])
            self.assertTrue(service.revoke_adaptation()["revoked"])
            clock_value[0] = start + timedelta(days=8)
            self._check_in(records, athlete_id, clock_value[0], pain=0)
            reset = service.preview_decision()
            self.assertEqual(reset["recommended_option"], "usual")
            self.assertEqual(reset["recommendation_basis"], "current low-risk report")

    def test_mcp_tools_keep_profiles_isolated_and_feedback_versioned(self) -> None:
        with TemporaryDirectory() as temporary:
            first = Path(temporary) / "first"
            second = Path(temporary) / "second"
            athlete_id, records, _ = self._init(first)
            self._init(second)
            now = datetime.now(UTC)
            self._check_in(records, athlete_id, now, pain=0)
            preview = server.call_tool("preview_training_decision", {}, first)
            with self.assertRaisesRegex(ValueError, "context changed"):
                server.call_tool(
                    "confirm_training_decision",
                    {"preview_hash": preview["preview_hash"], "selected_option": "usual"},
                    second,
                )
            confirmed = server.call_tool(
                "confirm_training_decision",
                {"preview_hash": preview["preview_hash"], "selected_option": "usual"},
                first,
            )
            decision_id = str(confirmed["decision_id"])
            saved = server.call_tool(
                "record_decision_outcome",
                {
                    "decision_id": decision_id,
                    "expected_revision": 0,
                    "completed": True,
                    "effect": "same",
                    "note": "",
                },
                first,
            )
            self.assertEqual(saved["feedback"]["revision"], 1)
            with self.assertRaisesRegex(ValueError, "revision changed"):
                server.call_tool(
                    "record_decision_outcome",
                    {
                        "decision_id": decision_id,
                        "expected_revision": 0,
                        "completed": True,
                        "effect": "worse",
                    },
                    first,
                )
            corrected = server.call_tool(
                "record_decision_outcome",
                {
                    "decision_id": decision_id,
                    "expected_revision": 1,
                    "completed": True,
                    "effect": "worse",
                },
                first,
            )
            self.assertEqual(corrected["feedback"]["revision"], 2)
            with self.assertRaises(KeyError):
                server.call_tool(
                    "record_decision_outcome",
                    {
                        "decision_id": decision_id,
                        "expected_revision": 0,
                        "completed": True,
                        "effect": "same",
                    },
                    second,
                )
            self.assertEqual(
                server.call_tool("get_growth_profile", {}, second)["outcome_count"], 0
            )

    def test_old_decision_feedback_does_not_become_recent_evidence(self) -> None:
        with TemporaryDirectory() as temporary:
            athlete_id, records, growth = self._init(Path(temporary) / "vault")
            first_day = datetime(2026, 1, 1, 8, tzinfo=UTC)
            clock_value = [first_day]
            service = GrowthDecisionService(
                athlete_id, records, growth, timezone_name="UTC", clock=lambda: clock_value[0]
            )
            self._check_in(records, athlete_id, first_day, pain=0)
            preview = service.preview_decision()
            decision = service.confirm_decision(str(preview["preview_hash"]), "easy")
            clock_value[0] = first_day + timedelta(days=100)
            saved = service.record_outcome(
                decision_id=str(decision["decision_id"]),
                expected_revision=0,
                completed=True,
                effect="better",
            )
            self.assertEqual(saved["growth_profile"]["outcome_count"], 0)
            history = service.list_decisions()
            self.assertEqual(history["decisions"][0]["feedback"]["effect"], "better")


if __name__ == "__main__":
    unittest.main()
