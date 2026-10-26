"""Command handlers composed from application services."""

from __future__ import annotations

import getpass
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from athlete_growth.adapters.files import FileDataSource
from athlete_growth.adapters.garmin_cn import GarminCnConnector
from athlete_growth.adapters.races import RaceFileConnector
from athlete_growth.adapters.storage import (
    ProfileRegistry,
    SQLiteAthleteProfileRepository,
    SQLiteRecordStore,
    VaultLayout,
)
from athlete_growth.application.ai import ModelConsent, ModelRouter, RoutingPolicy
from athlete_growth.application.check_in import CheckInService
from athlete_growth.application.data_quality import inspect_records
from athlete_growth.application.data_rights import DataRightsService
from athlete_growth.application.ingest import IngestionService
from athlete_growth.application.nutrition import NutritionPlanner
from athlete_growth.application.plan import TrainingPlanner
from athlete_growth.application.race import RaceStrategyService
from athlete_growth.application.recovery import ReturnToTrainingService
from athlete_growth.application.report import ReportService
from athlete_growth.domain.goals import Goal, GoalType, detect_conflicts
from athlete_growth.domain.nutrition import FuelingContext, NutritionProfile
from athlete_growth.domain.records import CanonicalRecord, JsonValue
from athlete_growth.domain.recovery import InjuryStatus
from athlete_growth.domain.training import TrainingConstraints
from athlete_growth.ports.data_source import SyncWindow
from athlete_growth.ports.model_provider import ModelRequest


@dataclass(frozen=True, slots=True)
class Runtime:
    home: Path
    athlete_id: str
    timezone_name: str
    layout: VaultLayout
    store: SQLiteRecordStore

    @classmethod
    def load(cls, home: Path) -> Runtime:
        registry = ProfileRegistry(home / "config.toml")
        athlete_id = registry.default_profile_id()
        if athlete_id is None:
            raise RuntimeError("data home is not initialized")
        layout = VaultLayout(home, athlete_id)
        if not layout.database_file.is_file():
            raise RuntimeError("default profile database is missing")
        profile = SQLiteAthleteProfileRepository(layout.database_file).get_by_id(athlete_id)
        if profile is None:
            raise RuntimeError("default profile record is missing")
        return cls(
            home,
            athlete_id,
            profile.timezone_name,
            layout,
            SQLiteRecordStore(layout.database_file),
        )


def execute(args) -> dict[str, object]:
    home = args.data_home.expanduser().resolve()
    if args.command == "restore":
        restored = DataRightsService(home).restore_profile(
            args.archive, args.target_home.expanduser().resolve()
        )
        ProfileRegistry(restored.home / "config.toml").register(restored.athlete_id)
        return {"athlete_id": restored.athlete_id, "profile_root": str(restored.profile_root)}
    runtime = Runtime.load(home)
    if args.command == "import-file":
        batch = FileDataSource().read(args.path)
        result = IngestionService(
            athlete_id=runtime.athlete_id,
            store=runtime.store,
            raw_dir=runtime.layout.raw,
        ).ingest(batch)
        return asdict(result)
    if args.command == "sync":
        if not 1 <= args.chunk_days <= 28:
            raise ValueError("Garmin China chunk days must be between 1 and 28")
        connector = GarminCnConnector(home=home)
        result = IngestionService(
            athlete_id=runtime.athlete_id,
            store=runtime.store,
            raw_dir=runtime.layout.raw,
        ).sync_chunked(
            connector,
            SyncWindow(args.start, args.end),
            chunk_days=args.chunk_days,
        )
        day_count = (args.end - args.start).days + 1
        chunk_count = (day_count + args.chunk_days - 1) // args.chunk_days
        return {"source": "garmin-cn", "chunks": chunk_count, **asdict(result)}
    if args.command == "garmin-cn-login":
        email = (args.email or input("Garmin China email: ")).strip()
        if not email:
            raise ValueError("Garmin China email must not be empty")
        password = getpass.getpass("Garmin China password (hidden): ")
        if not password:
            raise ValueError("Garmin China password must not be empty")
        connector = GarminCnConnector(home=home)
        identity = connector.authenticate(
            email=email,
            password=password,
            prompt_mfa=lambda: getpass.getpass("Garmin China MFA code (hidden): ").strip(),
        )
        password = ""
        return {"authenticated": True, **identity}
    if args.command == "garmin-cn-revoke":
        GarminCnConnector(home=home).revoke()
        return {"revoked": True, "region": "cn"}
    if args.command == "check-in":
        record = CheckInService(runtime.athlete_id, runtime.store).record(
            occurred_at=args.at,
            fatigue=args.fatigue,
            soreness=args.soreness,
            pain=args.pain,
            mood=args.mood,
            motivation=args.motivation,
            illness=args.illness,
            rpe=args.rpe,
            context={
                "work_stress": args.work_stress,
                "travel": args.travel,
                "alcohol": args.alcohol,
            },
        )
        return {"record_id": record.record_id, "data": record.data}
    if args.command == "today":
        return asdict(
            ReportService(
                runtime.athlete_id,
                runtime.store,
                timezone_name=runtime.timezone_name,
            ).daily(args.date)
        )
    if args.command == "weekly":
        return asdict(
            ReportService(
                runtime.athlete_id,
                runtime.store,
                timezone_name=runtime.timezone_name,
            ).weekly(args.end_date)
        )
    if args.command == "activity":
        records = runtime.store.list_current(runtime.athlete_id, kind="activity")
        return {"activities": [record_view(item) for item in records[-args.limit :]]}
    if args.command == "data-quality":
        records = runtime.store.list_current(runtime.athlete_id)
        findings = inspect_records(records)
        return {"findings": [asdict(item) for item in findings], "record_count": len(records)}
    if args.command == "goal-add":
        goal = Goal(
            str(uuid4()),
            runtime.athlete_id,
            args.type,
            args.title,
            args.priority,
            args.deadline,
            args.target_metric,
            args.target_value,
        )
        record = CanonicalRecord.create(
            athlete_id=runtime.athlete_id,
            kind="goal",
            occurred_at=datetime.now(UTC),
            data={
                "goal_id": goal.goal_id,
                "goal_type": goal.goal_type,
                "title": goal.title,
                "priority": goal.priority,
                "deadline": goal.deadline.isoformat() if goal.deadline else None,
                "target_metric": goal.target_metric,
                "target_value": goal.target_value,
                "status": goal.status,
            },
        )
        runtime.store.save(record)
        goals = _load_goals(runtime)
        return {"goal_id": goal.goal_id, "conflicts": [asdict(c) for c in detect_conflicts(goals)]}
    if args.command == "goal-list":
        goals = _load_goals(runtime)
        return {
            "goals": [asdict(goal) for goal in goals],
            "conflicts": [asdict(c) for c in detect_conflicts(goals)],
        }
    if args.command == "plan-create":
        goals = {goal.goal_id: goal for goal in _load_goals(runtime)}
        if args.goal_id not in goals:
            raise KeyError(f"unknown goal: {args.goal_id}")
        available = _weekday_minutes(args.available)
        unavailable = frozenset(_csv_ints(args.unavailable))
        constraints = TrainingConstraints(
            available,
            unavailable,
            args.max_weekly_minutes,
            (args.sport,),
        )
        report = ReportService(
            runtime.athlete_id,
            runtime.store,
            timezone_name=runtime.timezone_name,
        ).daily(args.start_date)
        plan = TrainingPlanner().generate(
            athlete_id=runtime.athlete_id,
            goal=goals[args.goal_id],
            constraints=constraints,
            safety=report.safety,
            start_date=args.start_date,
        )
        TrainingPlanner().save(plan, runtime.store)
        return asdict(plan)
    if args.command == "plan-list":
        records = runtime.store.list_current(runtime.athlete_id, kind="training-plan")
        return {"plans": [record.data for record in records]}
    if args.command == "export":
        service = DataRightsService(home)
        archive = service.export_profile(runtime.athlete_id, args.destination)
        verification = service.verify_archive(archive)
        return {"archive": str(archive), "verified": verification.valid}
    if args.command == "backup":
        archive = DataRightsService(home).backup_profile(runtime.athlete_id)
        return {"archive": str(archive), "verified": True}
    if args.command == "delete-profile":
        DataRightsService(home).delete_profile(runtime.athlete_id, confirmation=args.confirmation)
        ProfileRegistry(home / "config.toml").unregister(runtime.athlete_id)
        return {"deleted": runtime.athlete_id}
    if args.command == "recovery":
        injury = InjuryStatus(
            str(uuid4()), args.body_area, args.started_on, "recovering", args.notes
        )
        plan = ReturnToTrainingService().assess(
            injury=injury,
            pain=args.pain,
            swelling=args.swelling,
            normal_daily_function=args.normal_daily_function,
            stable_days=args.stable_days,
        )
        runtime.store.save(
            CanonicalRecord.create(
                athlete_id=runtime.athlete_id,
                kind="recovery-assessment",
                occurred_at=datetime.now(UTC),
                data=_record_data({"injury": injury, "plan": plan}),
            )
        )
        return asdict(plan)
    if args.command == "nutrition":
        profile = NutritionProfile(
            args.weight_kg,
            tuple(args.preference or ()),
            tuple(args.allergy or ()),
            args.sweat_rate,
        )
        context = FuelingContext(args.duration_min, args.intensity, args.race, args.temperature)
        plan = NutritionPlanner().plan(profile, context)
        runtime.store.save(
            CanonicalRecord.create(
                athlete_id=runtime.athlete_id,
                kind="nutrition-plan",
                occurred_at=datetime.now(UTC),
                data=_record_data({"profile": profile, "context": context, "plan": plan}),
            )
        )
        return asdict(plan)
    if args.command == "race-strategy":
        event = RaceFileConnector().read(args.event_file)[0]
        forecast: dict[str, float] | None = None
        if args.temperature is not None or args.wind_kph is not None:
            forecast = {
                "temperature_c": float(args.temperature or 0),
                "wind_kph": float(args.wind_kph or 0),
            }
        strategy = RaceStrategyService().build(
            event=event,
            target_finish_min=args.target_finish_min,
            forecast=forecast,
            travel_context={"timezone_shift_hours": args.timezone_shift},
        )
        output = RaceStrategyService().export_markdown(strategy, args.output)
        runtime.store.save(
            CanonicalRecord.create(
                athlete_id=runtime.athlete_id,
                kind="race-strategy",
                occurred_at=datetime.now(UTC),
                data=_record_data(strategy),
            )
        )
        return {"output": str(output), "strategy": asdict(strategy)}
    if args.command == "ai-explain":
        request = ModelRequest(
            "Use cited evidence and stay within general wellness boundaries.",
            args.question,
            tuple(args.evidence or ()),
            args.risk,
        )
        response = ModelRouter([]).generate(
            request,
            consent=ModelConsent(external_ai=False),
            policy=RoutingPolicy(Decimal("0"), "local_only"),
        )
        return asdict(response)
    raise AssertionError(f"unhandled command: {args.command}")


def record_view(record: CanonicalRecord) -> dict[str, object]:
    return {
        "record_id": record.record_id,
        "kind": record.kind,
        "occurred_at": record.occurred_at.isoformat(),
        "data": record.data,
        "evidence": [asdict(item) for item in record.evidence],
        "quality_flags": record.quality_flags,
    }


def _load_goals(runtime: Runtime) -> list[Goal]:
    goals: list[Goal] = []
    for record in runtime.store.list_current(runtime.athlete_id, kind="goal"):
        data = record.data
        deadline_value = data.get("deadline")
        goals.append(
            Goal(
                str(data["goal_id"]),
                runtime.athlete_id,
                cast(GoalType, str(data["goal_type"])),
                str(data["title"]),
                int(_numeric_scalar(data["priority"])),
                date.fromisoformat(str(deadline_value)) if deadline_value else None,
                str(data["target_metric"]) if data.get("target_metric") else None,
                float(_numeric_scalar(data["target_value"]))
                if data.get("target_value") is not None
                else None,
                str(data.get("status", "active")),
            )
        )
    return goals


def _weekday_minutes(value: str) -> dict[int, int]:
    result: dict[int, int] = {}
    for item in value.split(","):
        weekday, minutes = item.split(":", maxsplit=1)
        result[int(weekday)] = int(minutes)
    return result


def _csv_ints(value: str) -> list[int]:
    return [int(item) for item in value.split(",") if item.strip()]


def _record_data(value: object) -> dict[str, JsonValue]:
    normalized = _record_value(value)
    if not isinstance(normalized, dict):
        raise TypeError("canonical guidance data must be an object")
    return normalized


def _record_value(value: object) -> JsonValue:
    if is_dataclass(value) and not isinstance(value, type):
        return _record_value(asdict(cast(Any, value)))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _record_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_record_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def _numeric_scalar(value: JsonValue) -> str | int | float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("expected a numeric scalar")
    return value
