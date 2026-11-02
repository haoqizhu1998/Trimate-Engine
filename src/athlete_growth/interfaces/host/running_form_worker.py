"""Detached private running-form worker."""

from __future__ import annotations

import argparse
from pathlib import Path

from athlete_growth.interfaces.host.running_form import process_job


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-home", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    process_job(args.data_home, args.job_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
