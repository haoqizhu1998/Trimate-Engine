#!/usr/bin/env python3
"""Extract only activity FIT files from Garmin's UploadedFiles export archives."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

import fitdecode


def inspect_fit(payload: bytes) -> tuple[object, object, object]:
    """Return FIT file type, creation time, and product from its file_id message."""
    with fitdecode.FitReader(io.BytesIO(payload)) as reader:
        for frame in reader:
            if isinstance(frame, fitdecode.FitDataMessage) and frame.name == "file_id":
                values = {field.name: field.value for field in frame.fields}
                return (
                    values.get("type"),
                    values.get("time_created"),
                    values.get("garmin_product") or values.get("product"),
                )
    return None, None, None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    archives = sorted(args.archive_dir.glob("UploadedFiles_0-_Part*.zip"))
    if not archives:
        raise SystemExit(f"No UploadedFiles archives found in {args.archive_dir}")

    type_counts: Counter[str] = Counter()
    errors: list[dict[str, str]] = []
    manifest: list[dict[str, str | int]] = []
    seen_hashes: set[str] = set()

    for archive_path in archives:
        with ZipFile(archive_path) as archive:
            for member in archive.infolist():
                if member.is_dir() or not member.filename.lower().endswith(".fit"):
                    continue
                payload = archive.read(member)
                try:
                    file_type, created_at, product = inspect_fit(payload)
                except Exception as exc:  # Preserve failures in the report; do not upload them.
                    errors.append(
                        {
                            "archive": archive_path.name,
                            "source_name": member.filename,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    continue

                normalized_type = str(file_type)
                type_counts[normalized_type] += 1
                if file_type != "activity":
                    continue

                digest = hashlib.sha256(payload).hexdigest()
                if digest in seen_hashes:
                    type_counts["duplicate_activity"] += 1
                    continue
                seen_hashes.add(digest)

                destination = args.output_dir / Path(member.filename).name
                destination.write_bytes(payload)
                manifest.append(
                    {
                        "file": destination.name,
                        "bytes": len(payload),
                        "created_at_utc": created_at.isoformat() if created_at else "",
                        "product": str(product or ""),
                        "sha256": digest,
                        "source_archive": archive_path.name,
                    }
                )

    manifest.sort(key=lambda item: (item["created_at_utc"], item["file"]))
    with (args.output_dir / "manifest.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(manifest[0]) if manifest else ["file"])
        writer.writeheader()
        writer.writerows(manifest)

    report = {
        "archives": [path.name for path in archives],
        "fit_type_counts": dict(sorted(type_counts.items())),
        "activity_files_extracted": len(manifest),
        "activity_date_min_utc": manifest[0]["created_at_utc"] if manifest else None,
        "activity_date_max_utc": manifest[-1]["created_at_utc"] if manifest else None,
        "errors": errors,
    }
    (args.output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
