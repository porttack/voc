#!/usr/bin/env python3
"""
Read eCO2 and TVOC from the SGP30.

Usage:
    python3 read_voc.py                   # single reading
    python3 read_voc.py --count 10        # 10 readings, 1 s apart
    python3 read_voc.py --count 0         # run until Ctrl-C
    python3 read_voc.py --no-baseline     # skip loading saved baseline

A saved baseline in baseline.json is automatically loaded at startup if it
is less than 7 days old.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

from sgp30 import SGP30

BASELINE_FILE = "baseline.json"
BASELINE_MAX_AGE_DAYS = 7
WARMUP_SECONDS = 15


def load_baseline(sensor: SGP30) -> bool:
    if not os.path.exists(BASELINE_FILE):
        return False
    with open(BASELINE_FILE) as f:
        data = json.load(f)
    saved_at = datetime.fromisoformat(data["saved_at"])
    age_days = (datetime.now(timezone.utc) - saved_at).total_seconds() / 86400
    if age_days > BASELINE_MAX_AGE_DAYS:
        print(
            f"Baseline is {age_days:.1f} days old (limit {BASELINE_MAX_AGE_DAYS}); "
            "ignoring — sensor will self-calibrate over ~12 h.",
            file=sys.stderr,
        )
        return False
    sensor.set_baseline(data["eco2_baseline"], data["tvoc_baseline"])
    print(
        f"Loaded baseline (saved {age_days:.1f} days ago): "
        f"eCO2={data['eco2_baseline']}, TVOC={data['tvoc_baseline']}",
        file=sys.stderr,
    )
    return True


def main():
    parser = argparse.ArgumentParser(description="Read SGP30 VOC sensor")
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="Number of readings (0 = run until Ctrl-C, default 1)",
    )
    parser.add_argument(
        "--no-baseline",
        action="store_true",
        help="Do not load saved baseline from baseline.json",
    )
    args = parser.parse_args()

    with SGP30() as sensor:
        sensor.iaq_init()

        if not args.no_baseline:
            load_baseline(sensor)

        print("Warming up…", file=sys.stderr)
        for _ in range(WARMUP_SECONDS):
            sensor.measure_iaq()
            time.sleep(1)

        print(f"{'Timestamp':<26} {'eCO2 (ppm)':>10} {'TVOC (ppb)':>10}")
        print("-" * 50)

        n = 0
        try:
            while args.count == 0 or n < args.count:
                eco2, tvoc = sensor.measure_iaq()
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"{ts:<26} {eco2:>10} {tvoc:>10}")
                n += 1
                if args.count == 0 or n < args.count:
                    time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopped.", file=sys.stderr)


if __name__ == "__main__":
    main()
