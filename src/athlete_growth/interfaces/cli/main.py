"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, cast

from athlete_growth.adapters.storage import (
    ProfileRegistry,
    SQLiteAthleteProfileRepository,
    VaultLayout,
)
from athlete_growth.application import initialize_profile
from athlete_growth.application.automation import NotificationMode
from athlete_growth.domain.athlete import UnitSystem
from athlete_growth.interfaces.cli import commands


def _default_data_home() -> Path:
    configured = os.environ.get("ATHLETE_GROWTH_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".athlete-growth"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="athlete-growth",
        description="Local-first personal training analysis and growth toolkit",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    init_parser = subcommands.add_parser("init", help="initialize a local profile")
    init_parser.add_argument("--name", required=True, help="profile display name")
    init_parser.add_argument(
        "--timezone", required=True, help="IANA timezone, for example Asia/Shanghai"
    )
    init_parser.add_argument("--units", choices=("metric", "statute"), default="metric")
    init_parser.add_argument(
        "--data-home",
        type=Path,
        default=_default_data_home(),
        help="private data vault (default: ATHLETE_GROWTH_HOME or ~/.athlete-growth)",
    )
    status_parser = subcommands.add_parser("status", help="show the default profile")
    status_parser.add_argument(
        "--data-home",
        type=Path,
        default=_default_data_home(),
        help="private data vault (default: ATHLETE_GROWTH_HOME or ~/.athlete-growth)",
    )

    mcp_reload = subcommands.add_parser(
        "mcp-reload",
        help="request a Codex app-server MCP refresh when its control socket is reachable",
    )
    mcp_reload.add_argument(
        "--socket",
        type=Path,
        help="optional Codex app-server control socket; omitted uses the CLI daemon default",
    )
    mcp_reload.add_argument("--data-home", type=Path, default=_default_data_home())

    def add_home(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("--data-home", type=Path, default=_default_data_home())

    add_home(subcommands.add_parser(
        "workbuddy-configure", help="add the local training data connector to WorkBuddy"
    ))
    add_home(subcommands.add_parser(
        "workbuddy-unconfigure", help="remove only this product's WorkBuddy connector"
    ))

    automation_configure = subcommands.add_parser(
        "automation-configure",
        help="configure automatic device-source training updates",
    )
    automation_configure.add_argument(
        "--source", choices=("garmin-cn", "coros"), default="garmin-cn"
    )
    automation_configure.add_argument("--interval-minutes", type=int, default=30)
    automation_configure.add_argument("--lookback-days", type=int, default=3)
    automation_configure.add_argument(
        "--notification-mode",
        choices=tuple(mode.value for mode in NotificationMode),
        default=NotificationMode.ACTIONABLE_DIGEST.value,
    )
    automation_configure.add_argument(
        "--digest-hour",
        "--digest-hour-local",
        dest="digest_hour",
        type=int,
        default=20,
    )
    automation_configure.add_argument(
        "--disable",
        action="store_true",
        help="save the policy in a paused state",
    )
    add_home(automation_configure)

    automation_status = subcommands.add_parser(
        "automation-status",
        help="show safe automatic-update status and backlog counts",
    )
    add_home(automation_status)
    automation_run = subcommands.add_parser(
        "automation-run-once",
        help="process due or recoverable automatic-update work once",
    )
    automation_run.add_argument(
        "--force",
        action="store_true",
        help="run enabled policies now using an idempotent minute slot",
    )
    add_home(automation_run)
    automation_install = subcommands.add_parser(
        "automation-install-macos",
        help="install the private macOS launch agent",
    )
    add_home(automation_install)
    automation_uninstall = subcommands.add_parser(
        "automation-uninstall-macos",
        help="remove the private macOS launch agent",
    )
    add_home(automation_uninstall)
    add_home(subcommands.add_parser(
        "automation-install-windows", help="install the Windows per-user sync task"
    ))
    add_home(subcommands.add_parser(
        "automation-uninstall-windows", help="remove the Windows per-user sync task"
    ))
    notification_list = subcommands.add_parser(
        "notification-list",
        help="list local automation notifications",
    )
    notification_list.add_argument("--include-read", action="store_true")
    add_home(notification_list)
    notification_read = subcommands.add_parser(
        "notification-read",
        help="mark one local automation notification read",
    )
    notification_read.add_argument("notification_id", metavar="ID")
    add_home(notification_read)

    import_parser = subcommands.add_parser("import-file", help="import CSV/JSON/TCX/FIT")
    import_parser.add_argument("path", type=Path)
    add_home(import_parser)
    running_stage = subcommands.add_parser(
        "running-form-stage", help="stage a private running video for asynchronous analysis"
    )
    running_stage.add_argument("path", type=Path)
    running_stage.add_argument("--note", default="")
    add_home(running_stage)
    running_model = subcommands.add_parser(
        "running-form-install-model", help="install the official pose model in the private vault"
    )
    running_model.add_argument("--from-file", type=Path)
    add_home(running_model)
    running_setup = subcommands.add_parser(
        "running-form-setup", help="install optional pose-analysis dependencies with Python 3.12"
    )
    add_home(running_setup)
    running_status = subcommands.add_parser(
        "running-form-runtime", help="check the local pose-analysis backend"
    )
    add_home(running_status)

    sync_parser = subcommands.add_parser("sync", help="sync a read-only device source")
    sync_parser.add_argument("--source", choices=("garmin-cn", "coros"), default="garmin-cn")
    sync_parser.add_argument("--start", type=date.fromisoformat)
    sync_parser.add_argument("--end", type=date.fromisoformat)
    sync_parser.add_argument(
        "--backfill-existing",
        action="store_true",
        help="fill missing detail for activities already in the local Garmin archive",
    )
    sync_parser.add_argument(
        "--chunk-days",
        type=int,
        default=28,
        help="persist long syncs in restart-safe chunks (1-28, default: 28)",
    )
    add_home(sync_parser)

    login_parser = subcommands.add_parser(
        "garmin-cn-login", help="authenticate to Garmin Connect China and cache a session"
    )
    login_parser.add_argument("--email", help="Garmin China account email")
    add_home(login_parser)
    revoke_parser = subcommands.add_parser(
        "garmin-cn-revoke", help="remove Garmin China credentials"
    )
    add_home(revoke_parser)
    for action in ("helper-install", "login-start", "login-finish", "login-status", "revoke"):
        coros_parser = subcommands.add_parser(
            f"coros-{action}", help=f"run local COROS OAuth {action} via the official helper"
        )
        add_home(coros_parser)

    check_in = subcommands.add_parser("check-in", help="record subjective readiness")
    check_in.add_argument("--at", type=datetime.fromisoformat, required=True)
    for name in ("fatigue", "soreness", "pain", "mood", "motivation"):
        check_in.add_argument(f"--{name}", type=int, required=True)
    check_in.add_argument("--rpe", type=int)
    check_in.add_argument("--illness", action="store_true")
    check_in.add_argument("--work-stress", type=int, default=0)
    check_in.add_argument("--travel", action="store_true")
    check_in.add_argument("--alcohol", action="store_true")
    add_home(check_in)

    today = subcommands.add_parser("today", help="show deterministic daily report")
    today.add_argument("--date", type=date.fromisoformat, default=date.today())
    add_home(today)
    weekly = subcommands.add_parser("weekly", help="show deterministic weekly report")
    weekly.add_argument("--end-date", type=date.fromisoformat, default=date.today())
    add_home(weekly)
    activity = subcommands.add_parser("activity", help="list canonical activities")
    activity.add_argument("--limit", type=int, default=20)
    add_home(activity)
    quality = subcommands.add_parser("data-quality", help="inspect data quality")
    add_home(quality)

    goal_add = subcommands.add_parser("goal-add", help="add sport/body/race goal")
    goal_add.add_argument("--type", choices=("sport", "body", "race"), required=True)
    goal_add.add_argument("--title", required=True)
    goal_add.add_argument("--priority", type=int, required=True)
    goal_add.add_argument("--deadline", type=date.fromisoformat)
    goal_add.add_argument("--target-metric")
    goal_add.add_argument("--target-value", type=float)
    add_home(goal_add)
    goal_list = subcommands.add_parser("goal-list", help="list goals and conflicts")
    add_home(goal_list)

    plan_create = subcommands.add_parser("plan-create", help="create constrained plan")
    plan_create.add_argument("--goal-id", required=True)
    plan_create.add_argument("--start-date", type=date.fromisoformat, required=True)
    plan_create.add_argument("--available", required=True, help="weekday:minutes CSV")
    plan_create.add_argument("--unavailable", default="")
    plan_create.add_argument("--max-weekly-minutes", type=int, required=True)
    plan_create.add_argument("--sport", required=True)
    add_home(plan_create)
    plan_list = subcommands.add_parser("plan-list", help="list plan versions")
    add_home(plan_list)

    export_parser = subcommands.add_parser("export", help="export verified profile archive")
    export_parser.add_argument("destination", type=Path)
    add_home(export_parser)
    backup = subcommands.add_parser("backup", help="create verified profile backup")
    add_home(backup)
    restore = subcommands.add_parser("restore", help="restore verified profile archive")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--target-home", type=Path, required=True)
    add_home(restore)
    delete = subcommands.add_parser("delete-profile", help="delete one confirmed profile")
    delete.add_argument("--confirmation", required=True)
    add_home(delete)

    recovery = subcommands.add_parser("recovery", help="assess return-to-training stage")
    recovery.add_argument("--body-area", required=True)
    recovery.add_argument("--started-on", type=date.fromisoformat, required=True)
    recovery.add_argument("--pain", type=int, required=True)
    recovery.add_argument("--swelling", action="store_true")
    recovery.add_argument("--normal-daily-function", action="store_true")
    recovery.add_argument("--stable-days", type=int, default=0)
    recovery.add_argument("--notes", default="user reported")
    add_home(recovery)

    nutrition = subcommands.add_parser("nutrition", help="create general fueling plan")
    nutrition.add_argument("--weight-kg", type=float, required=True)
    nutrition.add_argument("--duration-min", type=int, required=True)
    nutrition.add_argument("--intensity", required=True)
    nutrition.add_argument("--race", action="store_true")
    nutrition.add_argument("--temperature", type=float)
    nutrition.add_argument("--sweat-rate", type=int)
    nutrition.add_argument("--preference", action="append")
    nutrition.add_argument("--allergy", action="append")
    add_home(nutrition)

    race = subcommands.add_parser("race-strategy", help="build offline race strategy")
    race.add_argument("event_file", type=Path)
    race.add_argument("--target-finish-min", type=float, required=True)
    race.add_argument("--output", type=Path, required=True)
    race.add_argument("--temperature", type=float)
    race.add_argument("--wind-kph", type=float)
    race.add_argument("--timezone-shift", type=float, default=0)
    add_home(race)

    ai = subcommands.add_parser("ai-explain", help="offline-safe explanation by default")
    ai.add_argument("question")
    ai.add_argument("--evidence", action="append")
    ai.add_argument("--risk", choices=("unknown", "low", "moderate", "high"), default="unknown")
    add_home(ai)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    home = args.data_home.expanduser().resolve()
    registry = ProfileRegistry(home / "config.toml")

    if args.command == "status":
        athlete_id = registry.default_profile_id()
        if athlete_id is None:
            print("Data home is not initialized. Run `athlete-growth init`.", file=sys.stderr)
            return 1
        layout = VaultLayout(home=home, athlete_id=athlete_id)
        if not layout.database_file.is_file():
            print("Default profile database is missing.", file=sys.stderr)
            return 1
        profile = SQLiteAthleteProfileRepository(layout.database_file).get_by_id(athlete_id)
        if profile is None:
            print("Default profile record is missing.", file=sys.stderr)
            return 1
        print(f"Profile: {profile.display_name}")
        print(f"Athlete ID: {profile.athlete_id}")
        print(f"Timezone: {profile.timezone_name}")
        print(f"Units: {profile.unit_system}")
        print(f"Private data vault: {layout.profile_root}")
        return 0

    if args.command != "init":
        try:
            result = commands.execute(args)
        except (KeyError, ValueError, RuntimeError, OSError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, default=_json_default))
        return 0

    def repository_factory(athlete_id: str) -> SQLiteAthleteProfileRepository:
        layout = VaultLayout(home=home, athlete_id=athlete_id)
        layout.ensure()
        return SQLiteAthleteProfileRepository(layout.database_file)

    profile = initialize_profile(
        display_name=args.name,
        timezone_name=args.timezone,
        unit_system=cast(UnitSystem, args.units),
        repository_factory=repository_factory,
        registry=registry,
    )
    layout = VaultLayout(home=home, athlete_id=profile.athlete_id)
    print(f"Profile initialized: {profile.athlete_id}")
    print(f"Private data vault: {layout.profile_root}")
    return 0


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(cast(Any, value))
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


if __name__ == "__main__":
    raise SystemExit(main())
