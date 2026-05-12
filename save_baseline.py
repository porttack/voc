#!/usr/bin/env python3
"""
Save the SGP30's current IAQ baseline to baseline.json.

The baseline should only be saved after the sensor has been running
continuously for at least 12 hours — before that the values are unreliable.
Run this script periodically (e.g. via cron every hour) once the sensor
is established.

Usage:
    python3 save_baseline.py
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

from sgp30 import SGP30

BASELINE_FILE = "baseline.json"


def main():
    with SGP30() as sensor:
        sensor.iaq_init()
        # Brief settle — does not replace full 12-hour calibration.
        time.sleep(0.5)
        eco2_base, tvoc_base = sensor.get_baseline()

    if eco2_base == 0 and tvoc_base == 0:
        print(
            "ERROR: Sensor returned zero baseline — it has not been running long enough. "
            "Leave it powered on for at least 12 hours before saving the baseline.",
            file=sys.stderr,
        )
        sys.exit(1)

    payload = {
        "eco2_baseline": eco2_base,
        "tvoc_baseline": tvoc_base,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = BASELINE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, BASELINE_FILE)  # atomic on POSIX — never a partial file

    print(f"Baseline saved to {BASELINE_FILE}")
    print(f"  eCO2 baseline : {eco2_base}")
    print(f"  TVOC baseline : {tvoc_base}")
    print(f"  Saved at      : {payload['saved_at']}")


if __name__ == "__main__":
    main()
