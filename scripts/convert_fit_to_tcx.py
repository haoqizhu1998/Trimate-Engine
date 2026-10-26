#!/usr/bin/env python3
"""Convert Garmin activity FIT files to Garmin-compatible TCX locally."""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path

import fitdecode

TCX_NS = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
TPX_NS = "http://www.garmin.com/xmlschemas/ActivityExtension/v2"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

ET.register_namespace("", TCX_NS)
ET.register_namespace("xsi", XSI_NS)
ET.register_namespace("ns", TPX_NS)


def q(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def add(parent: ET.Element, name: str, value: object | None) -> ET.Element | None:
    if value is None:
        return None
    element = ET.SubElement(parent, q(TCX_NS, name))
    element.text = str(value)
    return element


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def semicircles_to_degrees(value: int | float) -> float:
    return float(value) * 180.0 / (2**31)


def message_values(message: fitdecode.FitDataMessage) -> dict[str, object]:
    return {field.name: field.value for field in message.fields}


def read_fit(
    path: Path,
) -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]]]:
    file_id: dict[str, object] = {}
    sessions: list[dict[str, object]] = []
    records: list[dict[str, object]] = []
    with fitdecode.FitReader(path) as reader:
        for frame in reader:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            if frame.name == "file_id":
                file_id = message_values(frame)
            elif frame.name == "session":
                sessions.append(message_values(frame))
            elif frame.name == "record":
                values = message_values(frame)
                if isinstance(values.get("timestamp"), datetime):
                    records.append(values)
    return file_id, sessions, records


def tcx_sport(value: object) -> str:
    return {"running": "Running", "cycling": "Biking"}.get(str(value), "Other")


def session_records(
    records: list[dict[str, object]], session: dict[str, object]
) -> list[dict[str, object]]:
    start = session.get("start_time")
    end = session.get("timestamp")
    if isinstance(start, datetime) and (not isinstance(end, datetime) or end <= start):
        elapsed = session.get("total_elapsed_time", session.get("total_timer_time"))
        if isinstance(elapsed, (int, float)):
            end = start + timedelta(seconds=float(elapsed))
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return records
    selected = [
        record
        for record in records
        if isinstance(record.get("timestamp"), datetime) and start <= record["timestamp"] <= end
    ]
    return selected or records


def collapse_multisport_sessions(
    sessions: list[dict[str, object]], records: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Represent a FIT multisport event as one TCX activity with one track."""
    if len(sessions) <= 1:
        return sessions

    starts = [s.get("start_time") for s in sessions if isinstance(s.get("start_time"), datetime)]
    if not starts:
        return sessions

    start = min(starts)
    record_times = [r.get("timestamp") for r in records if isinstance(r.get("timestamp"), datetime)]
    end = max(record_times) if record_times else start
    timer_values = [float(s.get("total_timer_time") or 0) for s in sessions]
    weights = [value if value > 0 else 1.0 for value in timer_values]

    merged: dict[str, object] = {
        "sport": "other",
        "start_time": start,
        "timestamp": end,
        "total_timer_time": sum(timer_values),
        "total_elapsed_time": max(0.0, (end - start).total_seconds()),
        "total_distance": sum(float(s.get("total_distance") or 0) for s in sessions),
        "total_calories": sum(int(s.get("total_calories") or 0) for s in sessions),
    }
    heart_rates = [s.get("avg_heart_rate") for s in sessions]
    if all(isinstance(value, (int, float)) for value in heart_rates):
        merged["avg_heart_rate"] = sum(
            float(value) * weight for value, weight in zip(heart_rates, weights, strict=False)
        ) / sum(weights)
    maximum_heart_rates = [
        float(value)
        for s in sessions
        if isinstance((value := s.get("max_heart_rate")), (int, float))
    ]
    if maximum_heart_rates:
        merged["max_heart_rate"] = max(maximum_heart_rates)
    maximum_speeds = [
        float(value) for s in sessions if isinstance((value := s.get("max_speed")), (int, float))
    ]
    if maximum_speeds:
        merged["max_speed"] = max(maximum_speeds)
    return [merged]


def add_trackpoint(track: ET.Element, record: dict[str, object], sport: str) -> None:
    point = ET.SubElement(track, q(TCX_NS, "Trackpoint"))
    timestamp = record.get("timestamp")
    if isinstance(timestamp, datetime):
        add(point, "Time", iso_utc(timestamp))

    latitude = record.get("position_lat")
    longitude = record.get("position_long")
    if latitude is not None and longitude is not None:
        position = ET.SubElement(point, q(TCX_NS, "Position"))
        add(position, "LatitudeDegrees", f"{semicircles_to_degrees(latitude):.8f}")
        add(position, "LongitudeDegrees", f"{semicircles_to_degrees(longitude):.8f}")

    altitude = record.get("enhanced_altitude", record.get("altitude"))
    if altitude is not None:
        add(point, "AltitudeMeters", f"{float(altitude):.2f}")
    if record.get("distance") is not None:
        add(point, "DistanceMeters", f"{float(record['distance']):.2f}")

    if record.get("heart_rate") is not None:
        heart_rate = ET.SubElement(point, q(TCX_NS, "HeartRateBpm"))
        add(heart_rate, "Value", int(record["heart_rate"]))

    cadence = record.get("cadence")
    if cadence is not None and sport == "Biking":
        add(point, "Cadence", int(float(cadence)))

    speed = record.get("enhanced_speed", record.get("speed"))
    power = record.get("power")
    run_cadence = cadence if cadence is not None and sport == "Running" else None
    if speed is not None or power is not None or run_cadence is not None:
        extensions = ET.SubElement(point, q(TCX_NS, "Extensions"))
        tpx = ET.SubElement(extensions, q(TPX_NS, "TPX"))
        if speed is not None:
            speed_element = ET.SubElement(tpx, q(TPX_NS, "Speed"))
            speed_element.text = f"{float(speed):.3f}"
        if run_cadence is not None:
            cadence_element = ET.SubElement(tpx, q(TPX_NS, "RunCadence"))
            cadence_element.text = str(int(float(run_cadence)))
        if power is not None:
            watts = ET.SubElement(tpx, q(TPX_NS, "Watts"))
            watts.text = str(int(float(power)))


def convert(path: Path, destination: Path) -> None:
    file_id, sessions, records = read_fit(path)
    if not sessions:
        raise ValueError("FIT file has no session message")
    sessions = collapse_multisport_sessions(sessions, records)

    root = ET.Element(
        q(TCX_NS, "TrainingCenterDatabase"),
        {
            q(XSI_NS, "schemaLocation"): (
                f"{TCX_NS} http://www.garmin.com/xmlschemas/TrainingCenterDatabasev2.xsd"
            )
        },
    )
    activities = ET.SubElement(root, q(TCX_NS, "Activities"))

    for session in sessions:
        related_records = session_records(records, session)
        start = session.get("start_time")
        if not isinstance(start, datetime) and related_records:
            start = related_records[0].get("timestamp")
        if not isinstance(start, datetime):
            raise ValueError("FIT session has no start time")

        sport = tcx_sport(session.get("sport"))
        activity = ET.SubElement(activities, q(TCX_NS, "Activity"), {"Sport": sport})
        add(activity, "Id", iso_utc(start))

        lap = ET.SubElement(activity, q(TCX_NS, "Lap"), {"StartTime": iso_utc(start)})
        total_time = session.get("total_timer_time", session.get("total_elapsed_time", 0))
        add(lap, "TotalTimeSeconds", f"{float(total_time or 0):.3f}")
        add(lap, "DistanceMeters", f"{float(session.get('total_distance') or 0):.2f}")
        if session.get("max_speed") is not None:
            add(lap, "MaximumSpeed", f"{float(session['max_speed']):.3f}")
        add(lap, "Calories", int(session.get("total_calories") or 0))

        if session.get("avg_heart_rate") is not None:
            average_hr = ET.SubElement(lap, q(TCX_NS, "AverageHeartRateBpm"))
            add(average_hr, "Value", int(session["avg_heart_rate"]))
        if session.get("max_heart_rate") is not None:
            maximum_hr = ET.SubElement(lap, q(TCX_NS, "MaximumHeartRateBpm"))
            add(maximum_hr, "Value", int(session["max_heart_rate"]))

        add(lap, "Intensity", "Active")
        if sport == "Biking" and session.get("avg_cadence") is not None:
            add(lap, "Cadence", int(float(session["avg_cadence"])))
        add(lap, "TriggerMethod", "Manual")

        track = ET.SubElement(lap, q(TCX_NS, "Track"))
        for record in related_records:
            add_trackpoint(track, record, sport)

        creator = ET.SubElement(activity, q(TCX_NS, "Creator"), {q(XSI_NS, "type"): "Device_t"})
        add(creator, "Name", "Garmin Device")
        add(creator, "UnitId", int(file_id.get("serial_number") or 0))
        product = file_id.get("garmin_product", file_id.get("product"))
        add(creator, "ProductID", int(product) if isinstance(product, (int, float)) else 0)
        version = ET.SubElement(creator, q(TCX_NS, "Version"))
        add(version, "VersionMajor", 1)
        add(version, "VersionMinor", 0)
        add(version, "BuildMajor", 0)
        add(version, "BuildMinor", 0)

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    destination.parent.mkdir(parents=True, exist_ok=True)
    tree.write(destination, encoding="utf-8", xml_declaration=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()

    converted = 0
    skipped = 0
    failed: list[tuple[str, str]] = []
    for source in sorted(args.source_dir.glob("*.fit")):
        destination = args.output_dir / f"{source.stem}.tcx"
        if destination.exists() and destination.stat().st_size > 0:
            skipped += 1
            continue
        try:
            convert(source, destination)
            converted += 1
        except Exception as exc:
            failed.append((source.name, f"{type(exc).__name__}: {exc}"))

    print(f"converted={converted}")
    print(f"skipped={skipped}")
    print(f"failed={len(failed)}")
    for name, error in failed:
        print(f"{name}: {error}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
