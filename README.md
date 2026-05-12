# voc — SGP30 VOC / eCO2 sensor on Raspberry Pi

Real-time air quality monitoring for the **SLV Makerspace** (or any space) using a
Sensirion SGP30 sensor on a Raspberry Pi Zero.  Reads TVOC (ppb) and equivalent
CO₂ (ppm) over I²C, serves a live web dashboard, logs data to CSV, and sends
push alerts via **ntfy** when air quality degrades.

---

## Hardware

| SGP30 pin | Pi Zero GPIO |
|-----------|-------------|
| VCC       | 3.3 V (pin 1) |
| GND       | GND (pin 6) |
| SDA       | GPIO 2 / SDA (pin 3) |
| SCL       | GPIO 3 / SCL (pin 5) |

The SGP30 has a fixed I²C address of **0x58** — no address configuration needed.

> **Coming soon:** humidity/temperature sensor support (SHT31 or BME280 over I²C).
> Feeding absolute humidity into the SGP30 improves VOC accuracy.

---

## Quick start

```bash
git clone https://github.com/porttack/voc.git
cd voc
bash install.sh
```

`install.sh` will:
- Enable I²C if not already on
- Ensure NTP time sync (`systemd-timesyncd`) for trustworthy timestamps
- Install `python3-venv` and `i2c-tools` via apt
- Create `.venv/` and install `smbus2`, `flask`, and `tzdata` into it
- Rewrite script shebangs to the venv Python (no `source activate` needed)
- Add your user to the `i2c` group
- Generate and enable `voc.service` so the dashboard starts on every boot

After install, open a browser on the same network:

```
http://<pi-ip-address>:8080
```

Find your Pi's IP with `hostname -I`.

---

## ntfy push alerts

The service sends push notifications to **[ntfy.sh](https://ntfy.sh)** when air
quality reaches **Poor** or worse.  No account is required for the free hosted tier.

### Setup

1. Edit `config.json` in the repo directory:
   ```json
   {
     "ntfy_url": "https://ntfy.sh/slvmakerspace-voc",
     "ntfy_cooldown_minutes": 30
   }
   ```
   Pick any topic name you like — keep it hard to guess if you want it semi-private.

2. Restart the service to pick up the new config:
   ```bash
   sudo systemctl restart voc
   ```

3. Subscribe on your phone:
   - **iOS / Android:** install the [ntfy app](https://ntfy.sh) and subscribe to your topic
   - **Browser:** open `https://ntfy.sh/slvmakerspace-voc` and click Subscribe
   - **Self-hosted:** change `ntfy_url` to point at your own ntfy server

### What triggers an alert

| Condition | Priority | Action |
|-----------|----------|--------|
| TVOC ≥ 2200 ppb (Poor) | High | Alert sent |
| TVOC ≥ 5500 ppb (Very Poor) | **Urgent** | Alert sent |
| eCO₂ ≥ 1500 ppm (Poor) | High | Alert sent |
| eCO₂ ≥ 2000 ppm (Very Poor) | **Urgent** | Alert sent |
| Returns below Poor | Default | All-clear sent |

A re-alert fires every 30 minutes (configurable) while the space remains in a Poor
or worse state.  ntfy alerts are disabled if `ntfy_url` is left empty.

### What to watch for in a makerspace

Common causes of elevated VOC readings and what to do:

| Source | TVOC signature | Response |
|--------|---------------|----------|
| 3D printing (ABS/resin) | 1000–8000+ ppb during print | Run enclosure filter, open window |
| Laser cutting | Sharp spike, drops quickly after | Run exhaust fan during and 10 min after |
| Soldering | Moderate spike near station | Fume extractor at the iron |
| Spray paint / adhesives | Large spike, slow decay | Open all doors/windows |
| Cleaning products | Spike when applied | Ventilate, wait for it to clear |
| Many people + poor ventilation | Slow eCO₂ rise over hours | Open windows or run HVAC |

---

## Web dashboard

The dashboard auto-updates every 2 seconds and shows:

- **Current TVOC and eCO₂** with color-coded ratings and descriptions
- **Alert banner** when either reading reaches Poor or worse
- **Three chart panels** — each with Y-axis min/mid/max labels and X-axis time labels:
  - *Live (last 5 min)* — 1 Hz in-memory buffer
  - *Last 24 hours* — from the 5-minute CSV log
  - *Last 28 days* — hourly averages; shaded band shows full daily min–max range
- **Gaps in data** (sensor offline, Pi rebooted) appear as breaks in the line
- **Hover / touch** any chart to see the exact timestamp and value at that point
- **Download CSV** link at the bottom of the page

### Service management

```bash
sudo systemctl status voc        # running?
sudo systemctl restart voc       # restart (e.g. after editing config.json)
sudo journalctl -u voc -f        # live logs
sudo systemctl stop voc
sudo systemctl disable voc       # don't start on boot
```

---

## Command-line scripts

All scripts use the `.venv` Python automatically — no activation needed.

### `read_voc.py`

```bash
./read_voc.py               # single reading (15 s warm-up)
./read_voc.py --count 10    # 10 readings, 1 s apart
./read_voc.py --count 0     # run until Ctrl-C
./read_voc.py --no-baseline
```

### `monitor_voc.py`

```bash
./monitor_voc.py                     # continuous terminal output
./monitor_voc.py --log voc_log.csv   # also write CSV
```

### `save_baseline.py` / `load_baseline.py`

```bash
./save_baseline.py    # save current sensor calibration to baseline.json
./load_baseline.py    # inspect and restore baseline.json
```

---

## Data logging

Readings are saved every **5 minutes** to `~/.local/voc/voc.csv`:

```
timestamp,eco2_ppm,tvoc_ppb
2025-05-12T14:30:00-07:00,512,42
```

Timestamps use **America/Los_Angeles** (PST/PDT) from the system clock, kept
accurate by `systemd-timesyncd` (NTP).

### Cron — save baseline hourly (if not using the web service)

```cron
0 * * * * cd /home/pi/voc && ./save_baseline.py >> /var/log/sgp30_baseline.log 2>&1
```

---

## How the SGP30 baseline works

1. **First 15 s:** returns placeholder values (eCO₂ = 400 ppm, TVOC = 0 ppb).
2. **First 12 hours:** self-calibrates; readings improve steadily.
3. **Every hour:** baseline is saved to `baseline.json` automatically.
4. **On restart:** baseline is restored immediately — no re-calibration wait.
5. **After 7 days offline:** saved baseline expires; sensor recalibrates from scratch.
6. **1 Hz cadence required:** `measure_iaq()` must be called every second or the
   internal algorithm loses track.

---

## File reference

| File              | Purpose |
|-------------------|---------|
| `sgp30.py`        | Low-level SGP30 driver (smbus2, raw I²C) |
| `voc_web.py`      | Flask web dashboard + sensor loop + ntfy alerts |
| `config.json`     | ntfy URL and alert cooldown (edit this) |
| `read_voc.py`     | Single or repeated CLI readings |
| `monitor_voc.py`  | Continuous terminal monitoring with auto-baseline |
| `save_baseline.py`| Save current sensor baseline to `baseline.json` |
| `load_baseline.py`| Load `baseline.json` and apply to sensor |
| `baseline.json`   | Saved calibration (auto-created, not in git) |
| `install.sh`      | One-time setup: I²C, venv, packages, systemd service |
| `requirements.txt`| Python dependencies (`smbus2`, `flask`, `tzdata`) |
