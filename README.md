# voc — SGP30 VOC / eCO2 sensor on Raspberry Pi

Read Total Volatile Organic Compounds (TVOC, ppb) and equivalent CO₂ (eCO₂, ppm)
from a Sensirion SGP30 sensor connected over I²C to a Raspberry Pi Zero (or any Pi).

---

## Hardware

| SGP30 pin | Pi Zero GPIO |
|-----------|-------------|
| VCC       | 3.3 V (pin 1) |
| GND       | GND (pin 6) |
| SDA       | GPIO 2 / SDA (pin 3) |
| SCL       | GPIO 3 / SCL (pin 5) |

The SGP30 has a fixed I²C address of **0x58** — no address configuration needed.

---

## Quick start

```bash
git clone https://github.com/porttack/voc.git
cd voc
bash install.sh
# reboot if I2C was just enabled
sudo reboot
```

After reboot, confirm the sensor is visible:

```bash
i2cdetect -y 1
# You should see '58' in the grid
```

---

## Scripts

### `read_voc.py` — take a reading

```bash
# Single reading (15 s warm-up, then one line of output)
python3 read_voc.py

# 10 readings, 1 second apart
python3 read_voc.py --count 10

# Run until Ctrl-C
python3 read_voc.py --count 0

# Skip loading a saved baseline
python3 read_voc.py --no-baseline
```

### `monitor_voc.py` — continuous monitoring

Runs indefinitely at 1 Hz. Automatically loads `baseline.json` at startup
(if it exists and is less than 7 days old) and saves the baseline every hour.

```bash
# Print to stdout
python3 monitor_voc.py

# Also write a CSV log
python3 monitor_voc.py --log voc_log.csv

# Ignore any saved baseline
python3 monitor_voc.py --no-baseline
```

### `save_baseline.py` — persist calibration

Reads the current baseline from the sensor and writes it to `baseline.json`.
Only run this after the sensor has been **continuously powered for at least 12 hours**;
before that the baseline values are not yet meaningful.

```bash
python3 save_baseline.py
```

### `load_baseline.py` — inspect / restore baseline

Shows the contents of `baseline.json` and applies it to the sensor.
If the file is older than 7 days the baseline is not applied (the sensor
re-calibrates from scratch instead).

```bash
python3 load_baseline.py
```

---

## How the SGP30 baseline works

The SGP30 uses an internal algorithm that adapts to the background air
quality over time.  Understanding the calibration cycle helps you get
accurate readings:

1. **First power-on (no baseline):** The first 15 seconds return fixed
   placeholder values (eCO₂ = 400 ppm, TVOC = 0 ppb).  After that the
   sensor starts real estimates, but accuracy improves slowly over ~12 hours.

2. **After 12 hours of continuous operation:** The baseline is stable.
   Run `save_baseline.py` to persist it.  The monitor script does this
   automatically every hour.

3. **On next startup:** `read_voc.py` and `monitor_voc.py` load
   `baseline.json` automatically, giving you accurate readings right away
   instead of waiting another 12 hours.

4. **Baseline expiry:** If the Pi has been off for more than 7 days the
   saved baseline is discarded and the sensor recalibrates from scratch.
   A saved baseline that is ≤ 7 days old is always safe to use.

5. **Calling cadence:** `measure_iaq()` must be called **every 1 second**.
   The sensor's internal algorithm depends on this fixed rate.  All scripts
   honour this requirement.

---

## Cron example — save baseline hourly

```cron
0 * * * * cd /home/pi/voc && python3 save_baseline.py >> /var/log/sgp30_baseline.log 2>&1
```

---

## File reference

| File              | Purpose |
|-------------------|---------|
| `sgp30.py`        | Low-level SGP30 driver (smbus2, raw I²C) |
| `read_voc.py`     | Single or repeated VOC readings |
| `monitor_voc.py`  | Continuous 1 Hz monitoring with auto-baseline |
| `save_baseline.py`| Save current sensor baseline to `baseline.json` |
| `load_baseline.py`| Load `baseline.json` and apply to sensor |
| `baseline.json`   | Saved calibration (auto-created, not in git) |
| `install.sh`      | One-time setup: I²C enable + pip install |
| `requirements.txt`| Python dependencies (`smbus2`) |
