"""Versioned product capability surface, independent of any model host."""

from __future__ import annotations

CAPABILITY_VERSION = "1.18"

CAPABILITIES: tuple[dict[str, object], ...] = (
    {
        "domain": "device_connections",
        "tools": ["get_sync_status", "run_sync_now", "list_activities", "get_data_quality"],
        "local_only": [
            "garmin-cn-login", "garmin-cn-revoke", "coros-helper-install",
            "coros-login-start", "coros-login-finish", "coros-login-status",
            "coros-revoke", "sync", "import-file",
        ],
        "sources": [
            {
                "id": "garmin-cn",
                "personal_connection": "local_login",
                "product_ingestion": "poll_implemented",
            },
            {
                "id": "coros",
                "personal_connection": "official_mcp_oauth",
                "product_ingestion": "experimental_poll_requires_structured_mcp",
                "online_query": "available_in_authorized_mcp_hosts",
            },
        ],
        "note": (
            "Device sources are product capabilities independent of GPT, Codex, "
            "WorkBuddy or Coze. COROS local ingestion requires separately authorized "
            "official helper and machine-readable activity results."
        ),
    },
    {
        "domain": "onboarding",
        "tools": ["get_onboarding_status", "get_profile_summary"],
        "local_only": ["init", "status"],
    },
    {
        "domain": "data",
        "tools": ["list_activities", "get_data_quality"],
        "local_only": [
            "import-file",
            "activity",
            "data-quality",
            "export",
            "backup",
            "restore",
            "delete-profile",
        ],
    },
    {
        "domain": "garmin_cn",
        "tools": ["get_sync_status", "run_sync_now"],
        "local_only": ["garmin-cn-login", "garmin-cn-revoke", "sync"],
    },
    {"domain": "analysis", "tools": ["get_today_report", "get_weekly_report"], "local_only": []},
    {
        "domain": "periodic_summary",
        "tools": ["get_periodic_training_summary", "render_periodic_training_poster"],
        "local_only": [],
        "note": "Observed calendar records only; poster pixels are returned without private paths.",
    },
    {
        "domain": "endurance_performance",
        "tools": [
            "get_activity_detail",
            "analyze_session",
            "get_sport_baseline",
            "get_sport_calibration",
            "preview_sport_calibration",
            "confirm_sport_calibration",
            "get_training_load_context",
            "compare_plan_actual",
            "list_plan_execution",
            "preview_plan_execution",
            "confirm_plan_execution",
        ],
        "local_only": [],
        "note": "Descriptive, source-bounded analysis; no inferred thresholds or race predictions.",
    },
    {
        "domain": "check_in",
        "tools": ["get_recent_check_ins", "preview_check_in", "confirm_check_in"],
        "local_only": [],
    },
    {
        "domain": "training_memory",
        "tools": ["preview_training_memory", "confirm_training_memory", "list_training_memory"],
        "local_only": [],
        "note": (
            "Manual entries require exact preview confirmation; "
            "partial body status is not clearance."
        ),
    },
    {
        "domain": "training_preferences",
        "tools": [
            "get_training_preferences", "preview_training_preferences",
            "confirm_training_preferences",
        ],
        "local_only": [],
        "note": "Confirmed profile preferences are versioned and can inform new plan previews.",
    },
    {
        "domain": "running_form",
        "tools": [
            "get_running_form_runtime", "start_running_form_analysis",
            "get_running_form_job", "get_running_form_contact_sheet",
        ],
        "local_only": [
            "running-form-stage", "running-form-setup",
            "running-form-install-model", "running-form-runtime",
        ],
        "note": (
            "Video is staged locally; asynchronous TriMate scoring requires optional "
            "MediaPipe dependencies and a private pose model."
        ),
    },
    {
        "domain": "goals_and_plans",
        "tools": [
            "list_goals",
            "list_open_planning_drafts",
            "propose_goal",
            "confirm_goal",
            "preview_goal_change",
            "confirm_goal_change",
            "preview_plan",
            "preview_plan_blocks",
            "preview_plan_adjustment",
            "confirm_plan",
            "review_plan",
            "preview_plan_update",
            "list_plan_execution",
            "preview_plan_execution",
            "confirm_plan_execution",
            "list_training_plans",
        ],
        "local_only": ["goal-add", "plan-create", "plan-list"],
    },
    {
        "domain": "growth",
        "tools": [
            "list_growth_history", "analyze_response_samples", "get_growth_profile",
            "preview_training_decision", "confirm_training_decision",
            "list_training_decisions", "record_decision_outcome",
            "confirm_growth_adaptation", "revoke_growth_adaptation",
        ],
        "local_only": [],
    },
    {"domain": "recovery", "tools": ["assess_return_to_training"], "local_only": ["recovery"]},
    {
        "domain": "nutrition",
        "tools": ["preview_fueling_plan", "preview_fueling_logistics"],
        "local_only": ["nutrition"],
        "note": (
            "General duration-based guidance and arithmetic check of user-declared "
            "rehearsed fueling access; not a sport-specific prescription."
        ),
    },
    {
        "domain": "race",
        "tools": ["preview_race_strategy", "preview_race_segments"],
        "local_only": ["race-strategy"],
        "note": (
            "Running strategy plus arithmetic, user-supplied discipline targets "
            "for running, cycling, swimming and triathlon."
        ),
    },
    {
        "domain": "workbuddy_delivery",
        "tools": ["get_capability_catalog", "get_onboarding_status"],
        "local_only": ["workbuddy-configure", "workbuddy-unconfigure"],
        "note": (
            "Local MCP config is installed by the user package; WorkBuddy Skill upload "
            "and connector trust remain user actions."
        ),
    },
    {
        "domain": "notifications",
        "tools": ["list_notifications", "mark_notification_read"],
        "local_only": [
            "automation-configure",
            "automation-install-macos",
            "automation-uninstall-macos",
            "automation-install-windows",
            "automation-uninstall-windows",
        ],
    },
    {
        "domain": "ai",
        "tools": [],
        "local_only": ["ai-explain"],
        "note": (
            "The conversation host is the explanation layer; deterministic evidence and "
            "safety come from report tools. External provider routing needs separate consent."
        ),
    },
)


def capability_catalog() -> dict[str, object]:
    return {"version": CAPABILITY_VERSION, "capabilities": list(CAPABILITIES)}
