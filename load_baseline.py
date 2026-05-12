#!/usr/bin/env python3
"""
Load a previously saved IAQ baseline from baseline.json into the SGP30.

Useful when starting a long-running measurement session: loading a recent
baseline skips the ~12-hour cold-start calibration period.

Usage:
    python3 load_baseline.py
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

from sgp30 import SGP30

BASELINE_FILE = "baseline.json"
BASELINE_MAX_AGE_DAYS = 7


def main():
    if not os.path.exists(BASELINE_FILE):
        print(f"No baseline file found at '{BASELINE_FILE}'.")
        print("Run save_baseline.py after the sensor has been running for 12+ hours.")
        sys.exit(1)

    with open(BASELINE_FILE) as f:
        data = json.load(f)

    saved_at = datetime.fromisoformat(data["saved_at"])
    age_days = (datetime.now(timezone.utc) - saved_at).total_seconds() / 86400

    print(f"Baseline file  : {BASELINE_FILE}")
    print(f"  eCO2 baseline: {data['eco2_baseline']}")
    print(f"  TVOC baseline: {data['tvoc_baseline']}")
    print(f"  Saved at     : {data['saved_at']}")
    print(f"  Age          : {age_days:.1f} days")

    if age_days > BASELINE_MAX_AGE_DAYS:
        print(
            f"\nWARNING: Baseline is older than {BASELINE_MAX_AGE_DAYS} days. "
            "It will not be applied — the sensor must re-calibrate from scratch.",
            file=sys.stderr,
        )
        sys.exit(1)

    with SGP30() as sensor:
        sensor.iaq_init()
        time.sleep(0.5)
        sensor.set_baseline(data["eco2_baseline"], data["tvoc_baseline"])

    print("\nBaseline applied to sensor successfully.")


if __name__ == "__main__":
    main()
