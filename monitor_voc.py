#!/usr/bin/env python3
"""
Continuous SGP30 monitoring with automatic baseline management.

- Loads baseline.json on startup if it is less than 7 days old.
- Calls measure_iaq() every 1 s as required by the SGP30 algorithm.
- Saves the baseline to baseline.json every hour.
- Optionally appends readings to a CSV log file.

Usage:
    python3 monitor_voc.py                        # print to stdout
    python3 monitor_voc.py --log voc_log.csv      # also write CSV
    python3 monitor_voc.py --no-baseline          # ignore saved baseline
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

from sgp30 import SGP30

BASELINE_FILE = "baseline.json"
BASELINE_MAX_AGE_DAYS = 7
BASELINE_SAVE_INTERVAL = 3600   # seconds between baseline saves
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
            f"[baseline] {age_days:.1f} days old — too stale, will self-calibrate.",
            file=sys.stderr,
        )
        return False
    sensor.set_baseline(data["eco2_baseline"], data["tvoc_baseline"])
    print(
        f"[baseline] Loaded (age {age_days:.1f} d): "
        f"eCO2={data['eco2_baseline']}, TVOC={data['tvoc_baseline']}",
        file=sys.stderr,
    )
    return True


def save_baseline(sensor: SGP30):
    eco2_base, tvoc_base = sensor.get_baseline()
    if eco2_base == 0 and tvoc_base == 0:
        return
    payload = {
        "eco2_baseline": eco2_base,
        "tvoc_baseline": tvoc_base,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(BASELINE_FILE, "w") as f:
        json.dump(payload, f, indent=2)
    print(
        f"[baseline] Saved: eCO2={eco2_base}, TVOC={tvoc_base}",
        file=sys.stderr,
    )


def main():
    parser = argparse.ArgumentParser(description="Continuous SGP30 monitoring")
    parser.add_argument("--log", metavar="FILE", help="Append readings to CSV file")
    parser.add_argument(
        "--no-baseline", action="store_true", help="Ignore saved baseline"
    )
    args = parser.parse_args()

    csv_file = None
    csv_writer = None
    if args.log:
        write_header = not os.path.exists(args.log)
        csv_file = open(args.log, "a", newline="")
        csv_writer = csv.writer(csv_file)
        if write_header:
            csv_writer.writerow(["timestamp", "eco2_ppm", "tvoc_ppb"])

    try:
        with SGP30() as sensor:
            sensor.iaq_init()

            if not args.no_baseline:
                load_baseline(sensor)

            print(f"Warming up for {WARMUP_SECONDS} s…", file=sys.stderr)
            for _ in range(WARMUP_SECONDS):
                sensor.measure_iaq()
                time.sleep(1)

            print(f"{'Timestamp':<26} {'eCO2 (ppm)':>10} {'TVOC (ppb)':>10}")
            print("-" * 50)

            last_save = time.monotonic()

            while True:
                eco2, tvoc = sensor.measure_iaq()
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"{ts:<26} {eco2:>10} {tvoc:>10}")

                if csv_writer:
                    csv_writer.writerow([ts, eco2, tvoc])
                    csv_file.flush()

                if time.monotonic() - last_save >= BASELINE_SAVE_INTERVAL:
                    save_baseline(sensor)
                    last_save = time.monotonic()

                time.sleep(1)

    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
    finally:
        if csv_file:
            csv_file.close()


if __name__ == "__main__":
    main()
