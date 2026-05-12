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

## Configuration

Settings live in **`config.py`** in the repo directory.  This file is committed
to git and contains safe defaults — never put secrets in it.

To override any setting without touching the committed file, create:

```
~/.config/voc/config.py
```

That file is never committed to git.  Put only the keys you want to change:

```python
# ~/.config/voc/config.py
NTFY_URL    = "https://ntfy.sh/my-private-topic"
GSHEET_ID   = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
GSHEET_CREDENTIALS = "/home/pi/.config/voc/credentials.json"
GSHEET_WRITE = True
```

Restart the service after any config change:

```bash
sudo systemctl restart voc
```

### All configuration keys

| Key | Default | Description |
|-----|---------|-------------|
| `PORT` | `8080` | Web dashboard port |
| `NTFY_URL` | `"https://ntfy.sh/slvmakerspace-voc"` | ntfy topic URL; `""` disables alerts |
| `NTFY_COOLDOWN_MINUTES` | `30` | Minutes between repeated alerts while bad |
| `GSHEET_WRITE` | `False` | Append every 5-min reading to a Google Sheet |
| `GSHEET_ID` | `""` | Spreadsheet ID from its URL |
| `GSHEET_WORKSHEET` | `"Sheet1"` | Tab name inside the spreadsheet |
| `GSHEET_CREDENTIALS` | `""` | Absolute path to service account JSON key |
| `GSHEET_READ` | `False` | Dashboard mode — read charts from Sheet, no sensor needed |

---

## ntfy push alerts

The service sends push notifications to **[ntfy.sh](https://ntfy.sh)** when air
quality reaches **Poor** or worse.  No account is required for the free hosted tier.

### Setup

1. Edit `~/.config/voc/config.py` (or `config.py` in the repo directory):
   ```python
   NTFY_URL = "https://ntfy.sh/my-space-air"   # pick any topic name
   ```
   Keep the topic name hard to guess if you want it semi-private.

2. Restart the service to pick up the new config:
   ```bash
   sudo systemctl restart voc
   ```

3. Subscribe on your phone:
   - **iOS / Android:** install the [ntfy app](https://ntfy.sh) and subscribe to your topic
   - **Browser:** open your topic URL and click Subscribe
   - **Self-hosted:** change `NTFY_URL` to point at your own ntfy server

### What triggers an alert

| Condition | Priority | Action |
|-----------|----------|--------|
| TVOC ≥ 2200 ppb (Poor) | High | Alert sent |
| TVOC ≥ 5500 ppb (Very Poor) | **Urgent** | Alert sent |
| eCO₂ ≥ 1500 ppm (Poor) | High | Alert sent |
| eCO₂ ≥ 2000 ppm (Very Poor) | **Urgent** | Alert sent |
| Returns below Poor | Default | All-clear sent |

A re-alert fires every 30 minutes (configurable) while the space remains in a Poor
or worse state.  ntfy alerts are disabled if `NTFY_URL` is left empty.

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

## Google Sheets integration

Two optional modes let you use a Google Sheet for persistent storage and
multi-device dashboards.

### Write mode — log readings to a Sheet

Every 5-minute reading is appended to a Google Sheet *in addition* to the local CSV.

**One-time setup:**

1. Create a Google Cloud project and enable the **Google Sheets API**.
2. Create a **service account** and download its JSON key file.
3. **Share** your spreadsheet with the service account e-mail address (Editor role).
4. Install the extra Python packages:
   ```bash
   cd /path/to/voc
   .venv/bin/pip install gspread google-auth
   ```
   (Or uncomment those lines in `requirements.txt` and re-run `install.sh`.)
5. Add to `~/.config/voc/config.py`:
   ```python
   GSHEET_WRITE       = True
   GSHEET_ID          = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
   GSHEET_WORKSHEET   = "Sheet1"
   GSHEET_CREDENTIALS = "/home/pi/.config/voc/credentials.json"
   ```
6. Restart the service: `sudo systemctl restart voc`

The spreadsheet will receive rows in the format:
```
timestamp,eco2_ppm,tvoc_ppb
2025-05-12T14:30:00-07:00,512,42
```

### Read / dashboard mode — no sensor required

When `GSHEET_READ = True`, the dashboard reads all chart data from the Google Sheet
instead of the local CSV.  The live 5-minute chart is hidden because no sensor data
is available.  This is useful for a second Pi (or laptop) acting as a display.

Requires `GSHEET_ID`, `GSHEET_WORKSHEET`, and `GSHEET_CREDENTIALS` to be set.

```python
# ~/.config/voc/config.py on the display-only machine
GSHEET_READ        = True
GSHEET_ID          = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
GSHEET_WORKSHEET   = "Sheet1"
GSHEET_CREDENTIALS = "/home/pi/.config/voc/credentials.json"
```

---

## Web dashboard

The dashboard auto-updates every 2 seconds and shows:

- **Current TVOC and eCO₂** with color-coded ratings and descriptions
- **Alert banner** when either reading reaches Poor or worse
- **Three chart panels** — each with Y-axis min/mid/max labels and X-axis time labels:
  - *Live (last 5 min)* — 1 Hz in-memory buffer (hidden in `GSHEET_READ` mode)
  - *Last 24 hours* — from the 5-minute CSV log (or Google Sheet)
  - *Last 28 days* — hourly averages; shaded band shows full daily min–max range
- **Gaps in data** (sensor offline, Pi rebooted) appear as breaks in the line
- **Hover / touch** any chart to see the exact timestamp and value at that point
- **Download CSV** link at the bottom of the page

### Service management

```bash
sudo systemctl status voc        # running?
sudo systemctl restart voc       # restart (e.g. after editing config)
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
| `config.py`       | All settings with defaults (edit or override via `~/.config/voc/config.py`) |
| `read_voc.py`     | Single or repeated CLI readings |
| `monitor_voc.py`  | Continuous terminal monitoring with auto-baseline |
| `save_baseline.py`| Save current sensor baseline to `baseline.json` |
| `load_baseline.py`| Load `baseline.json` and apply to sensor |
| `baseline.json`   | Saved calibration (auto-created, not in git) |
| `install.sh`      | One-time setup: I²C, venv, packages, systemd service |
| `requirements.txt`| Python dependencies (`smbus2`, `flask`, `tzdata`; optional `gspread`) |
