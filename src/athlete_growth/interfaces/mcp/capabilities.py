"""Narrow JSON-RPC tool schemas and validation for product capabilities."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path

from athlete_growth.interfaces.host import calibration as calibration_host
from athlete_growth.interfaces.host import capabilities as host
from athlete_growth.interfaces.host import execution as execution_host
from athlete_growth.interfaces.host import growth as growth_host
from athlete_growth.interfaces.host import memory as memory_host
from athlete_growth.interfaces.host import periodic as periodic_host
from athlete_growth.interfaces.host import preferences as preference_host
from athlete_growth.interfaces.host import running_form as running_form_host
from athlete_growth.interfaces.host import tools as operations
from athlete_growth.interfaces.host.catalog import capability_catalog

_CHECK_IN_FIELDS = {
    "occurred_at": {"type": "string", "format": "date-time"},
    "fatigue": {"type": "integer", "minimum": 0, "maximum": 10},
    "soreness": {"type": "integer", "minimum": 0, "maximum": 10},
    "pain": {"type": "integer", "minimum": 0, "maximum": 10},
    "mood": {"type": "integer", "minimum": 0, "maximum": 10},
    "motivation": {"type": "integer", "minimum": 0, "maximum": 10},
    "illness": {"type": "boolean"},
    "rpe": {"type": "integer", "minimum": 0, "maximum": 10},
    "work_stress": {"type": "integer", "minimum": 0, "maximum": 10},
    "travel": {"type": "boolean"},
    "alcohol": {"type": "boolean"},
}
_CHECK_IN_REQUIRED = ("occurred_at", "fatigue", "soreness", "pain", "mood", "motivation", "illness")

_PREFERENCE_CHANGES = {
    "type": "object",
    "minProperties": 1,
    "maxProperties": 5,
    "properties": {
        "key_session_pattern": {
            "type": ["string", "null"],
            "enum": ["focus_blocks", "mixed", None],
        },
        "aerobic_mixing": {"type": ["boolean", "null"]},
        "preferred_quality_weekday": {
            "type": ["integer", "null"],
            "minimum": 0,
            "maximum": 6,
        },
        "preferred_long_weekday": {
            "type": ["integer", "null"],
            "minimum": 0,
            "maximum": 6,
        },
        "focus_sport": {
            "type": ["string", "null"],
            "enum": ["running", "cycling", "swimming", None],
        },
    },
    "additionalProperties": False,
}

# name -> (description, JSON Schema properties, required fields, read-only)
TOOL_SPECS: dict[str, tuple[str, dict[str, object], tuple[str, ...], bool]] = {
    "get_capability_catalog": (
        "List supported conversational capabilities and operations intentionally kept local-only.",
        {},
        (),
        True,
    ),
    "get_onboarding_status": (
        "Find the next safe step for a new or returning athlete without exposing credentials.",
        {},
        (),
        True,
    ),
    "get_profile_summary": (
        "Read display name, timezone, units and profile age; never returns the private vault path.",
        {},
        (),
        True,
    ),
    "list_activities": (
        "Read bounded activity summaries, not raw Garmin payloads, routes or credential data.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
        (),
        True,
    ),
    "get_data_quality": (
        "Inspect canonical record quality and bounded findings before interpreting trends.",
        {},
        (),
        True,
    ),
    "list_training_plans": (
        "Read saved training plan versions for review or comparison.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 10}},
        (),
        True,
    ),
    "list_growth_history": (
        "Read bounded saved feedback and milestones; association is not causation.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
        (),
        True,
    ),
    "get_activity_detail": (
        "Read one current athlete-owned activity and bounded lap details without raw routes.",
        {"record_id": {"type": "string"}},
        ("record_id",),
        True,
    ),
    "analyze_session": (
        "Describe one running, cycling or swimming session using observed data only; "
        "triathlon has no combined pace.",
        {"record_id": {"type": "string"}},
        ("record_id",),
        True,
    ),
    "get_sport_baseline": (
        "Summarize observed sport history; never treats a median as a threshold "
        "or race prediction.",
        {
            "sport": {"type": "string", "enum": ["running", "cycling", "swimming", "triathlon"]},
            "days": {"type": "integer", "minimum": 7, "maximum": 365},
        },
        ("sport",),
        True,
    ),
    "get_sport_calibration": (
        "Read the user's confirmed sport test reference and its age, provenance and broad bands.",
        {"sport": {"type": "string", "enum": ["running", "cycling", "swimming"]}},
        ("sport",),
        True,
    ),
    "preview_sport_calibration": (
        "Preview a user-declared sport test reference; broad bands are not prescriptions.",
        {
            "sport": {"type": "string", "enum": ["running", "cycling", "swimming"]},
            "value": {"type": "number", "exclusiveMinimum": 0},
            "tested_on": {"type": "string", "format": "date"},
            "protocol": {"type": "string", "enum": [
                "field_test", "lab_test", "race_result", "user_estimate",
            ]},
            "source_activity_id": {"type": "string"},
        },
        ("sport", "value", "tested_on", "protocol"),
        True,
    ),
    "confirm_sport_calibration": (
        "Save only the sport test reference that the user reviewed and approved.",
        {
            "sport": {"type": "string", "enum": ["running", "cycling", "swimming"]},
            "value": {"type": "number", "exclusiveMinimum": 0},
            "tested_on": {"type": "string", "format": "date"},
            "protocol": {"type": "string", "enum": [
                "field_test", "lab_test", "race_result", "user_estimate",
            ]},
            "source_activity_id": {"type": "string"},
            "preview_hash": {"type": "string"},
        },
        ("sport", "value", "tested_on", "protocol", "preview_hash"),
        False,
    ),
    "get_training_load_context": (
        "Compare recent training by sport and by score method; load ratios are contextual, "
        "never injury predictions.",
        {
            "sport": {"type": "string", "enum": ["running", "cycling", "swimming", "triathlon"]},
            "end_date": {"type": "string", "format": "date"},
        },
        (),
        True,
    ),
    "get_recent_check_ins": (
        "Read bounded recent subjective fatigue, soreness, pain and recovery reports; "
        "missing entries remain unknown.",
        {"days": {"type": "integer", "minimum": 7, "maximum": 90}},
        (),
        True,
    ),
    "get_periodic_training_summary": (
        "Summarize one calendar period from the current athlete's observed records; "
        "never invents plan completion or personal records.",
        {
            "cycle_type": {
                "type": "string",
                "enum": ["week", "4weeks", "month", "quarter", "year"],
            },
            "anchor_date": {"type": "string", "format": "date"},
        },
        ("cycle_type",),
        True,
    ),
    "render_periodic_training_poster": (
        "Render the TriMate training summary poster from the current athlete's observed period; "
        "returns image pixels without a private file path.",
        {
            "cycle_type": {
                "type": "string",
                "enum": ["week", "4weeks", "month", "quarter", "year"],
            },
            "anchor_date": {"type": "string", "format": "date"},
        },
        ("cycle_type",),
        True,
    ),
    "preview_training_memory": (
        "Validate and display a manual activity, body status, goal note or preference "
        "before saving.",
        {
            "kind": {
                "type": "string",
                "enum": ["activity", "body_status", "goal_note", "preference"],
            },
            "occurred_at": {"type": "string", "format": "date-time"},
            "fields": {"type": "object", "maxProperties": 10},
        },
        ("kind", "occurred_at", "fields"),
        True,
    ),
    "confirm_training_memory": (
        "Save only the exact manual training memory the user reviewed and approved.",
        {
            "kind": {
                "type": "string",
                "enum": ["activity", "body_status", "goal_note", "preference"],
            },
            "occurred_at": {"type": "string", "format": "date-time"},
            "fields": {"type": "object", "maxProperties": 10},
            "preview_hash": {"type": "string"},
        },
        ("kind", "occurred_at", "fields", "preview_hash"),
        False,
    ),
    "list_training_memory": (
        "Read bounded recent manual training and body observations for the current profile.",
        {
            "days": {"type": "integer", "minimum": 1, "maximum": 365},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        (),
        True,
    ),
    "get_training_preferences": (
        "Read the athlete's confirmed structured planning preferences and their provenance.",
        {},
        (),
        True,
    ),
    "preview_training_preferences": (
        "Preview a private structured preference change. Null clears a field; "
        "no profile state changes until confirmation.",
        {"changes": _PREFERENCE_CHANGES},
        ("changes",),
        True,
    ),
    "confirm_training_preferences": (
        "Save exactly the displayed preference change after explicit user approval.",
        {"changes": _PREFERENCE_CHANGES, "preview_hash": {"type": "string"}},
        ("changes", "preview_hash"),
        False,
    ),
    "get_running_form_runtime": (
        "Check whether local video analysis dependencies and the pose model are installed.",
        {},
        (),
        True,
    ),
    "start_running_form_analysis": (
        "Start asynchronous analysis of a locally staged video; accepts only an opaque job ID.",
        {"job_id": {"type": "string", "pattern": "^rfj_[0-9a-f]{32}$"}},
        ("job_id",),
        False,
    ),
    "get_running_form_job": (
        "Read this profile's video analysis status and evidence-based result, if completed.",
        {"job_id": {"type": "string", "pattern": "^rfj_[0-9a-f]{32}$"}},
        ("job_id",),
        True,
    ),
    "get_running_form_contact_sheet": (
        "Return annotated evidence frames for a completed running-form analysis, if available.",
        {"job_id": {"type": "string", "pattern": "^rfj_[0-9a-f]{32}$"}},
        ("job_id",),
        True,
    ),
    "compare_plan_actual": (
        "Compare a specified planned session with a specified activity; "
        "does not infer completion or user effort.",
        {
            "plan_id": {"type": "string"},
            "session_id": {"type": "string"},
            "activity_id": {"type": "string"},
        },
        ("plan_id", "session_id", "activity_id"),
        True,
    ),
    "list_plan_execution": (
        "Read confirmed feedback and same-day or adjacent-day activity-to-session suggestions "
        "for one plan; "
        "matching never confirms completion.",
        {"plan_id": {"type": "string"}},
        ("plan_id",),
        True,
    ),
    "preview_plan_execution": (
        "Preview a planned-session outcome and optional activity link for athlete confirmation.",
        {
            "plan_id": {"type": "string"},
            "session_id": {"type": "string"},
            "status": {"type": "string", "enum": [
                "completed", "partial", "skipped", "substituted",
            ]},
            "activity_id": {"type": "string"},
            "rpe": {"type": "integer", "minimum": 0, "maximum": 10},
            "pain_after": {"type": "integer", "minimum": 0, "maximum": 10},
            "reason": {"type": "string", "maxLength": 500},
        },
        ("plan_id", "session_id", "status"),
        True,
    ),
    "confirm_plan_execution": (
        "Save the exact execution feedback after the athlete has reviewed and approved it.",
        {
            "plan_id": {"type": "string"},
            "session_id": {"type": "string"},
            "status": {"type": "string", "enum": [
                "completed", "partial", "skipped", "substituted",
            ]},
            "activity_id": {"type": "string"},
            "rpe": {"type": "integer", "minimum": 0, "maximum": 10},
            "pain_after": {"type": "integer", "minimum": 0, "maximum": 10},
            "reason": {"type": "string", "maxLength": 500},
            "preview_hash": {"type": "string"},
        },
        ("plan_id", "session_id", "status", "preview_hash"),
        False,
    ),
    "preview_check_in": (
        "Validate and display a subjective check-in before saving it. Does not write data.",
        _CHECK_IN_FIELDS,
        _CHECK_IN_REQUIRED,
        True,
    ),
    "confirm_check_in": (
        "Save only the exact check-in the user reviewed and explicitly approved. "
        "Requires the preview hash and all original fields.",
        {**_CHECK_IN_FIELDS, "preview_hash": {"type": "string"}},
        (*_CHECK_IN_REQUIRED, "preview_hash"),
        False,
    ),
    "assess_return_to_training": (
        "Non-diagnostic return-to-training assessment using user-reported symptoms. "
        "Stop progression and recommend professional review on red flags.",
        {
            "body_area": {"type": "string"},
            "started_on": {"type": "string", "format": "date"},
            "pain": {"type": "integer", "minimum": 0, "maximum": 10},
            "swelling": {"type": "boolean"},
            "normal_daily_function": {"type": "boolean"},
            "stable_days": {"type": "integer", "minimum": 0},
        },
        ("body_area", "started_on", "pain", "swelling", "normal_daily_function", "stable_days"),
        True,
    ),
    "preview_fueling_plan": (
        "Create general training/race fueling guidance without saving it. "
        "Not medical nutrition care.",
        {
            "weight_kg": {"type": "number", "exclusiveMinimum": 0},
            "duration_min": {"type": "integer", "minimum": 1},
            "intensity": {"type": "string", "enum": ["easy", "moderate", "hard"]},
            "is_race": {"type": "boolean"},
            "temperature_c": {"type": "number"},
            "sweat_rate_ml_per_hour": {"type": "integer", "minimum": 1},
            "preferences": {"type": "array", "items": {"type": "string"}},
            "allergies": {"type": "array", "items": {"type": "string"}},
        },
        ("weight_kg", "duration_min", "intensity", "is_race"),
        True,
    ),
    "preview_fueling_logistics": (
        "Check whether user-declared rehearsed carbohydrate rate, servings and "
        "access opportunities fit together; arithmetic only, not a prescription.",
        {
            "duration_min": {"type": "integer", "minimum": 1, "maximum": 1440},
            "declared_tested_carbs_g_per_hour": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": 180,
            },
            "carbs_per_serving_g": {"type": "number", "exclusiveMinimum": 0, "maximum": 200},
            "available_servings": {"type": "integer", "minimum": 0, "maximum": 60},
            "opportunity_times_min": {
                "type": "array",
                "maxItems": 60,
                "items": {"type": "integer", "minimum": 1},
            },
        },
        (
            "duration_min",
            "declared_tested_carbs_g_per_hour",
            "carbs_per_serving_g",
            "available_servings",
            "opportunity_times_min",
        ),
        True,
    ),
    "preview_race_strategy": (
        "Preview a running-race pace strategy from user-provided, unverified event details. "
        "Cycling, swimming and triathlon strategies are not yet supported; "
        "never fetches a file path.",
        {
            "sport": {"type": "string", "enum": ["running", "cycling", "swimming", "triathlon"]},
            "event_name": {"type": "string"},
            "event_date": {"type": "string", "format": "date"},
            "distance_km": {"type": "number", "exclusiveMinimum": 0},
            "target_finish_min": {"type": "number", "exclusiveMinimum": 0},
            "elevation_gain_m": {"type": "number", "minimum": 0},
            "location": {"type": "string"},
            "temperature_c": {"type": "number"},
            "wind_kph": {"type": "number", "minimum": 0},
            "timezone_shift_hours": {"type": "number"},
        },
        ("sport", "event_name", "event_date", "distance_km", "target_finish_min"),
        True,
    ),
    "preview_race_segments": (
        "Calculate discipline-specific targets from user-supplied distances and times; "
        "triathlon includes both transitions. No performance prediction or save.",
        {
            "event_name": {"type": "string", "maxLength": 200},
            "event_date": {"type": "string", "format": "date"},
            "sport": {"type": "string", "enum": ["running", "cycling", "swimming", "triathlon"]},
            "segments": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "sport": {
                            "type": "string",
                            "enum": ["running", "cycling", "swimming", "transition"],
                        },
                        "distance_km": {"type": "number", "exclusiveMinimum": 0},
                        "target_duration_min": {"type": "number", "exclusiveMinimum": 0},
                    },
                    "required": ["sport", "target_duration_min"],
                    "additionalProperties": False,
                },
            },
        },
        ("event_name", "event_date", "sport", "segments"),
        True,
    ),
    "analyze_response_samples": (
        "Calculate a descriptive correlation from 3–100 user-provided sample pairs; "
        "never infer causation.",
        {
            "stimulus_name": {"type": "string"},
            "outcome_name": {"type": "string"},
            "samples": {
                "type": "array",
                "minItems": 3,
                "maxItems": 100,
                "items": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 2,
                    "items": {"type": "number"},
                },
            },
        },
        ("stimulus_name", "outcome_name", "samples"),
        True,
    ),
    "get_growth_profile": (
        "Read the private, evidence-bounded 90-day decision response profile and any "
        "adaptation candidate. A candidate is not active until explicitly confirmed.",
        {},
        (),
        True,
    ),
    "preview_training_decision": (
        "Preview today's safety-bounded training choices using the current report and "
        "approved adaptation. Does not save or change a plan.",
        {},
        (),
        True,
    ),
    "confirm_training_decision": (
        "Save only a displayed, current decision preview after the user explicitly "
        "chooses an allowed option. Does not change a confirmed training plan.",
        {
            "preview_hash": {"type": "string"},
            "selected_option": {"type": "string", "enum": ["usual", "easy", "pause"]},
        },
        ("preview_hash", "selected_option"),
        False,
    ),
    "list_training_decisions": (
        "Read the recent decision journal and user-reported outcomes.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
        (),
        True,
    ),
    "record_decision_outcome": (
        "Record user-reported completion and response for a confirmed current decision "
        "only when the user supplies the feedback. Revision 0 creates; later edits "
        "require the returned revision.",
        {
            "decision_id": {"type": "string"},
            "expected_revision": {"type": "integer", "minimum": 0},
            "completed": {"type": "boolean"},
            "effect": {"type": "string", "enum": ["better", "same", "worse"]},
            "pain_after": {"type": "integer", "minimum": 0, "maximum": 10},
            "note": {"type": "string", "maxLength": 300},
        },
        ("decision_id", "expected_revision", "completed", "effect"),
        False,
    ),
    "confirm_growth_adaptation": (
        "Activate only the displayed, evidence-backed low-risk recommendation adaptation "
        "after the user explicitly approves it. Never changes safety rules or plans.",
        {"adaptation_id": {"type": "string"}},
        ("adaptation_id",),
        False,
    ),
    "revoke_growth_adaptation": (
        "Turn off the current recommendation adaptation at the user's request; "
        "preserve the decision history.",
        {},
        (),
        False,
    ),
    "mark_notification_read": (
        "Mark one local Inbox item read only after the user asks to dismiss it.",
        {"notification_id": {"type": "string"}},
        ("notification_id",),
        False,
    ),
    "run_sync_now": (
        "Run one idempotent device-source sync/analyze pass now, only at the user's request.",
        {},
        (),
        False,
    ),
}


def tool_catalog() -> list[dict[str, object]]:
    return [
        {
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": list(required),
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": read_only, "destructiveHint": False},
        }
        for name, (description, properties, required, read_only) in TOOL_SPECS.items()
    ]


def call_tool(name: str, arguments: Mapping[str, object], data_home: Path) -> dict[str, object]:
    spec = TOOL_SPECS[name]
    _, properties, required, _ = spec
    if set(arguments) - set(properties) or set(required) - set(arguments):
        raise ValueError("unsupported or missing tool argument")
    if name == "get_capability_catalog":
        return capability_catalog()
    if name == "get_onboarding_status":
        return host.onboarding_status(data_home)
    if name == "get_profile_summary":
        return host.profile_summary(data_home)
    if name == "list_activities":
        return host.activities(data_home, limit=_optional_int(arguments, "limit", 10))
    if name == "get_data_quality":
        return host.data_quality(data_home)
    if name == "list_training_plans":
        return host.training_plans(data_home, limit=_optional_int(arguments, "limit", 5))
    if name == "list_growth_history":
        return host.growth_history(data_home, limit=_optional_int(arguments, "limit", 10))
    if name == "get_activity_detail":
        return host.activity_detail(data_home, _str(arguments, "record_id"))
    if name == "analyze_session":
        return host.session_analysis(data_home, _str(arguments, "record_id"))
    if name == "get_sport_baseline":
        return host.sport_baseline(
            data_home,
            _str(arguments, "sport"),
            days=_optional_int(arguments, "days", 90),
        )
    if name == "get_sport_calibration":
        return calibration_host.get_sport_calibration(data_home, _str(arguments, "sport"))
    if name in {"preview_sport_calibration", "confirm_sport_calibration"}:
        sport = _str(arguments, "sport")
        value = _number(arguments, "value")
        tested_on = _date(arguments, "tested_on")
        protocol = _str(arguments, "protocol")
        source_activity_id = (
            _str(arguments, "source_activity_id") if "source_activity_id" in arguments else None
        )
        if name == "preview_sport_calibration":
            return calibration_host.preview_sport_calibration(
                data_home, sport, value, tested_on, protocol, source_activity_id
            )
        return calibration_host.confirm_sport_calibration(
            data_home, sport, value, tested_on, protocol,
            _str(arguments, "preview_hash"), source_activity_id,
        )
    if name == "get_training_load_context":
        return host.training_load_context(
            data_home,
            sport=_str(arguments, "sport") if "sport" in arguments else None,
            end_date=_date(arguments, "end_date") if "end_date" in arguments else None,
        )
    if name == "get_recent_check_ins":
        return host.recent_check_ins(data_home, days=_optional_int(arguments, "days", 28))
    if name in {"get_periodic_training_summary", "render_periodic_training_poster"}:
        cycle = _str(arguments, "cycle_type")
        anchor = _date(arguments, "anchor_date") if "anchor_date" in arguments else None
        if name == "get_periodic_training_summary":
            return periodic_host.periodic_summary(data_home, cycle, anchor)
        return periodic_host.periodic_poster(data_home, cycle, anchor)
    if name in {"preview_training_memory", "confirm_training_memory"}:
        fields = arguments["fields"]
        if not isinstance(fields, dict) or len(fields) > 10:
            raise ValueError("memory fields are invalid")
        kind = _str(arguments, "kind")
        occurred_at = _datetime(arguments, "occurred_at")
        if name == "preview_training_memory":
            return memory_host.preview_training_memory(data_home, kind, occurred_at, fields)
        return memory_host.confirm_training_memory(
            data_home, kind, occurred_at, fields, _str(arguments, "preview_hash")
        )
    if name == "list_training_memory":
        return memory_host.list_training_memory(
            data_home,
            days=_optional_int(arguments, "days", 28),
            limit=_optional_int(arguments, "limit", 30),
        )
    if name == "get_training_preferences":
        return preference_host.get_training_preferences(data_home)
    if name in {"preview_training_preferences", "confirm_training_preferences"}:
        changes = arguments["changes"]
        if not isinstance(changes, dict):
            raise ValueError("training preference fields are invalid")
        if name == "preview_training_preferences":
            return preference_host.preview_training_preferences(data_home, changes)
        return preference_host.confirm_training_preferences(
            data_home, changes, _str(arguments, "preview_hash")
        )
    if name == "get_running_form_runtime":
        return running_form_host.runtime_status(data_home)
    if name == "start_running_form_analysis":
        return running_form_host.start_analysis(data_home, _str(arguments, "job_id"))
    if name == "get_running_form_job":
        return running_form_host.get_job(data_home, _str(arguments, "job_id"))
    if name == "get_running_form_contact_sheet":
        return running_form_host.get_contact_sheet(data_home, _str(arguments, "job_id"))
    if name == "compare_plan_actual":
        return host.plan_actual_comparison(
            data_home,
            _str(arguments, "plan_id"),
            _str(arguments, "session_id"),
            _str(arguments, "activity_id"),
        )
    if name == "list_plan_execution":
        return execution_host.list_plan_execution(data_home, _str(arguments, "plan_id"))
    if name in {"preview_plan_execution", "confirm_plan_execution"}:
        values = {
            "plan_id": _str(arguments, "plan_id"),
            "session_id": _str(arguments, "session_id"),
            "status": _str(arguments, "status"),
            "activity_id": _str(arguments, "activity_id") if "activity_id" in arguments else None,
            "rpe": _int(arguments, "rpe") if "rpe" in arguments else None,
            "pain_after": _int(arguments, "pain_after") if "pain_after" in arguments else None,
            "reason": _str(arguments, "reason") if "reason" in arguments else None,
        }
        if name == "preview_plan_execution":
            return execution_host.preview_plan_execution(data_home, **values)
        return execution_host.confirm_plan_execution(
            data_home, preview_hash=_str(arguments, "preview_hash"), **values
        )
    if name in {"preview_check_in", "confirm_check_in"}:
        values: dict[str, object] = {
            "occurred_at": _datetime(arguments, "occurred_at"),
            "fatigue": _int(arguments, "fatigue"),
            "soreness": _int(arguments, "soreness"),
            "pain": _int(arguments, "pain"),
            "mood": _int(arguments, "mood"),
            "motivation": _int(arguments, "motivation"),
            "illness": _bool(arguments, "illness"),
            "rpe": _int(arguments, "rpe") if "rpe" in arguments else None,
            "work_stress": _optional_int(arguments, "work_stress", 0),
            "travel": _bool(arguments, "travel") if "travel" in arguments else False,
            "alcohol": _bool(arguments, "alcohol") if "alcohol" in arguments else False,
        }
        if name == "preview_check_in":
            return host.preview_check_in(data_home, **values)  # type: ignore[arg-type]
        return host.confirm_check_in(
            data_home, preview_hash=_str(arguments, "preview_hash"), **values
        )
    if name == "assess_return_to_training":
        return host.assess_return_to_training(
            body_area=_str(arguments, "body_area"),
            started_on=_date(arguments, "started_on"),
            pain=_int(arguments, "pain"),
            swelling=_bool(arguments, "swelling"),
            normal_daily_function=_bool(arguments, "normal_daily_function"),
            stable_days=_int(arguments, "stable_days"),
        )
    if name == "preview_fueling_plan":
        return host.preview_fueling_plan(
            weight_kg=_number(arguments, "weight_kg"),
            duration_min=_int(arguments, "duration_min"),
            intensity=_str(arguments, "intensity"),
            is_race=_bool(arguments, "is_race"),
            temperature_c=_number(arguments, "temperature_c")
            if "temperature_c" in arguments
            else None,
            sweat_rate_ml_per_hour=_int(arguments, "sweat_rate_ml_per_hour")
            if "sweat_rate_ml_per_hour" in arguments
            else None,
            preferences=_string_tuple(arguments, "preferences"),
            allergies=_string_tuple(arguments, "allergies"),
        )
    if name == "preview_fueling_logistics":
        raw_times = arguments["opportunity_times_min"]
        if not isinstance(raw_times, list) or len(raw_times) > 60:
            raise ValueError("fueling opportunities are invalid")
        if any(type(item) is not int for item in raw_times):
            raise ValueError("fueling opportunities are invalid")
        return host.preview_fueling_logistics(
            duration_min=_int(arguments, "duration_min"),
            declared_tested_carbs_g_per_hour=_number(arguments, "declared_tested_carbs_g_per_hour"),
            carbs_per_serving_g=_number(arguments, "carbs_per_serving_g"),
            available_servings=_int(arguments, "available_servings"),
            opportunity_times_min=raw_times,
        )
    if name == "preview_race_strategy":
        return host.preview_race_strategy(
            sport=_str(arguments, "sport"),
            event_name=_str(arguments, "event_name"),
            event_date=_date(arguments, "event_date"),
            distance_km=_number(arguments, "distance_km"),
            target_finish_min=_number(arguments, "target_finish_min"),
            elevation_gain_m=_number(arguments, "elevation_gain_m")
            if "elevation_gain_m" in arguments
            else 0.0,
            location=_str(arguments, "location") if "location" in arguments else "",
            temperature_c=_number(arguments, "temperature_c")
            if "temperature_c" in arguments
            else None,
            wind_kph=_number(arguments, "wind_kph") if "wind_kph" in arguments else None,
            timezone_shift_hours=_number(arguments, "timezone_shift_hours")
            if "timezone_shift_hours" in arguments
            else 0.0,
        )
    if name == "preview_race_segments":
        raw_segments = arguments["segments"]
        if not isinstance(raw_segments, list) or not 1 <= len(raw_segments) <= 5:
            raise ValueError("race segments are invalid")
        segments: list[dict[str, object]] = []
        for segment in raw_segments:
            if not isinstance(segment, dict) or set(segment) - {
                "sport",
                "distance_km",
                "target_duration_min",
            }:
                raise ValueError("race segments are invalid")
            if "sport" not in segment or "target_duration_min" not in segment:
                raise ValueError("race segments are invalid")
            segments.append(segment)
        return host.preview_race_segments(
            event_name=_str(arguments, "event_name"),
            event_date=_date(arguments, "event_date"),
            sport=_str(arguments, "sport"),
            segments=segments,
        )
    if name == "analyze_response_samples":
        raw = arguments["samples"]
        if not isinstance(raw, list) or not 3 <= len(raw) <= 100:
            raise ValueError("provide 3 to 100 sample pairs")
        samples: list[tuple[float, float]] = []
        for pair in raw:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError("each sample must contain two numbers")
            samples.append((_finite_number(pair[0]), _finite_number(pair[1])))
        return host.analyze_response_samples(
            stimulus_name=_str(arguments, "stimulus_name"),
            outcome_name=_str(arguments, "outcome_name"),
            samples=samples,
        )
    if name == "get_growth_profile":
        return growth_host.get_growth_profile(data_home)
    if name == "preview_training_decision":
        return growth_host.preview_training_decision(data_home)
    if name == "confirm_training_decision":
        return growth_host.confirm_training_decision(
            data_home, _str(arguments, "preview_hash"), _str(arguments, "selected_option")
        )
    if name == "list_training_decisions":
        return growth_host.list_training_decisions(data_home, _optional_int(arguments, "limit", 10))
    if name == "record_decision_outcome":
        return growth_host.record_decision_outcome(
            data_home,
            decision_id=_str(arguments, "decision_id"),
            expected_revision=_int(arguments, "expected_revision"),
            completed=_bool(arguments, "completed"),
            effect=_str(arguments, "effect"),
            pain_after=_int(arguments, "pain_after") if "pain_after" in arguments else None,
            note=_optional_note(arguments),
        )
    if name == "confirm_growth_adaptation":
        return growth_host.confirm_growth_adaptation(data_home, _str(arguments, "adaptation_id"))
    if name == "revoke_growth_adaptation":
        return growth_host.revoke_growth_adaptation(data_home)
    if name == "mark_notification_read":
        return operations.read_notification(data_home, _str(arguments, "notification_id"))
    if name == "run_sync_now":
        return operations.run_automation_once(data_home, force=True)
    raise ValueError("unknown capability tool")


def _str(arguments: Mapping[str, object], key: str) -> str:
    value = arguments[key]
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise ValueError(f"{key} must be nonempty text")
    return value


def _optional_note(arguments: Mapping[str, object]) -> str:
    value = arguments.get("note", "")
    if not isinstance(value, str) or len(value) > 300:
        raise ValueError("note must be text of at most 300 characters")
    return value


def _int(arguments: Mapping[str, object], key: str) -> int:
    value = arguments[key]
    if type(value) is not int:
        raise ValueError(f"{key} must be an integer")
    return value


def _optional_int(arguments: Mapping[str, object], key: str, default: int) -> int:
    return _int(arguments, key) if key in arguments else default


def _bool(arguments: Mapping[str, object], key: str) -> bool:
    value = arguments[key]
    if type(value) is not bool:
        raise ValueError(f"{key} must be a boolean")
    return value


def _finite_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("expected a finite number")
    return result


def _number(arguments: Mapping[str, object], key: str) -> float:
    return _finite_number(arguments[key])


def _date(arguments: Mapping[str, object], key: str) -> date:
    return date.fromisoformat(_str(arguments, key))


def _datetime(arguments: Mapping[str, object], key: str) -> datetime:
    return datetime.fromisoformat(_str(arguments, key))


def _string_tuple(arguments: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = arguments.get(key, [])
    if (
        not isinstance(value, list)
        or len(value) > 20
        or any(not isinstance(item, str) or len(item) > 100 for item in value)
    ):
        raise ValueError(f"{key} must be a short list of text")
    return tuple(value)
