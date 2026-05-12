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
```

`install.sh` will:
- Enable I²C if not already on
- Install `python3-venv` and `i2c-tools` via apt
- Create a virtualenv at `.venv/` and install `smbus2` and `flask` into it (system Python is untouched)
- Add your user to the `i2c` group
- Install and start a **systemd service** (`voc.service`) that runs the web dashboard automatically on boot

After running, open a browser on the same network and go to:

```
http://<pi-ip-address>:8080
```

You can find your Pi's IP with `hostname -I`.

> **First run:** the sensor warms up for 15 seconds before showing real readings.
> Accuracy improves over the first hour; full calibration takes ~12 hours.

---

## Web dashboard

The dashboard auto-updates every 2 seconds and shows:

- **TVOC** and **eCO₂** readings with color-coded air quality ratings
- Sparkline charts of the last 5 minutes
- Plain-English explanations of what the numbers mean and why they matter
- Threshold reference tables

### Service management

```bash
sudo systemctl status voc        # is it running?
sudo systemctl restart voc       # restart after a code change
sudo journalctl -u voc -f        # live logs
sudo systemctl stop voc          # stop the service
sudo systemctl disable voc       # don't start on boot
```

---

## Running scripts manually

All scripts run inside the virtualenv. Either activate it first:

```bash
source .venv/bin/activate
python3 read_voc.py
deactivate
```

Or call the venv Python directly without activating:

```bash
.venv/bin/python3 read_voc.py
```

## Command-line scripts

These are useful for one-off readings or cron jobs and work independently
of the web service.

### `read_voc.py` — take a reading

```bash
python3 read_voc.py              # single reading (15 s warm-up)
python3 read_voc.py --count 10   # 10 readings, 1 s apart
python3 read_voc.py --count 0    # run until Ctrl-C
python3 read_voc.py --no-baseline
```

### `monitor_voc.py` — continuous terminal monitoring

```bash
python3 monitor_voc.py                     # stdout
python3 monitor_voc.py --log voc_log.csv   # also log to CSV
```

### `save_baseline.py` / `load_baseline.py`

```bash
python3 save_baseline.py    # persist calibration to baseline.json
python3 load_baseline.py    # inspect baseline.json and apply to sensor
```

---

## How the SGP30 baseline works

1. **First power-on:** The first 15 s return placeholder values (eCO₂ = 400 ppm,
   TVOC = 0 ppb). After that readings begin, but accuracy improves over ~12 hours.

2. **After 12 hours:** Run `save_baseline.py` (or let the web service do it
   automatically every hour). The saved baseline is restored on the next startup,
   giving accurate readings immediately.

3. **Baseline expiry:** A baseline saved more than 7 days ago is discarded; the
   sensor recalibrates from scratch.

4. **Calling cadence:** `measure_iaq()` must be called **every 1 second** — the
   sensor's internal algorithm depends on this fixed rate. All scripts honour it.

---

## Cron example — save baseline hourly (if not using the web service)

```cron
0 * * * * cd /home/pi/voc && python3 save_baseline.py >> /var/log/sgp30_baseline.log 2>&1
```

---

## File reference

| File              | Purpose |
|-------------------|---------|
| `sgp30.py`        | Low-level SGP30 driver (smbus2, raw I²C) |
| `voc_web.py`      | Flask web dashboard — runs as a systemd service |
| `read_voc.py`     | Single or repeated VOC readings (CLI) |
| `monitor_voc.py`  | Continuous 1 Hz terminal monitoring with auto-baseline |
| `save_baseline.py`| Save current sensor baseline to `baseline.json` |
| `load_baseline.py`| Load `baseline.json` and apply to sensor |
| `baseline.json`   | Saved calibration (auto-created, not in git) |
| `install.sh`      | One-time setup: I²C, packages, systemd service |
| `requirements.txt`| Python dependencies (`smbus2`, `flask`) |
