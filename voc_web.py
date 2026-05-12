#!/usr/bin/env python3
"""SGP30 VOC web dashboard — live air quality monitor."""

import collections
import csv
import json
import os
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, send_file

# ── Config ────────────────────────────────────────────────────────────────────
@dataclass
class _Cfg:
    port:                  int  = 8080
    ntfy_url:              str  = ""
    ntfy_cooldown_minutes: int  = 30
    gsheet_write:          bool = False
    gsheet_id:             str  = ""
    gsheet_worksheet:      str  = "Sheet1"
    gsheet_credentials:    str  = ""
    gsheet_read:           bool = False

    @property
    def ntfy_cooldown(self) -> float:
        return self.ntfy_cooldown_minutes * 60


def _load_config() -> _Cfg:
    """Load config.py, then overlay ~/.config/voc/config.py if present."""
    cfg = _Cfg()
    paths = [Path("config.py"), Path.home() / ".config" / "voc" / "config.py"]
    for path in paths:
        if not path.exists():
            continue
        try:
            ns: dict = {}
            exec(compile(path.read_text(), str(path), "exec"), {}, ns)
            for key, val in ns.items():
                attr = key.lower()
                if not key.startswith("_") and hasattr(cfg, attr):
                    setattr(cfg, attr, val)
            print(f"Config loaded: {path}", flush=True)
        except Exception as exc:
            print(f"Config error ({path}): {exc}", file=sys.stderr, flush=True)
    return cfg


cfg = _load_config()

# ── Constants ─────────────────────────────────────────────────────────────────
BASELINE_FILE    = "baseline.json"
BASELINE_MAX_AGE = 7           # days
WARMUP_SECONDS   = 15
MAX_LIVE_HISTORY = 300         # 5 min at 1 Hz
LOG_DIR          = Path.home() / ".local" / "voc"
LOG_FILE         = LOG_DIR / "voc.csv"
LOG_INTERVAL     = 300         # seconds between CSV / Sheet rows
TZ               = ZoneInfo("America/Los_Angeles")

# ── Shared state ──────────────────────────────────────────────────────────────
app = Flask(__name__)
_lock    = threading.Lock()
_history: collections.deque = collections.deque(maxlen=MAX_LIVE_HISTORY)
_state:   dict = {"eco2": None, "tvoc": None, "ts": None, "phase": "starting"}

_ntfy_state = {"tvoc_lvl": 0, "eco2_lvl": 0, "last_sent": 0.0}

# ── Baseline ──────────────────────────────────────────────────────────────────
def _load_baseline(sensor) -> None:
    if not os.path.exists(BASELINE_FILE):
        return
    try:
        with open(BASELINE_FILE) as f:
            data = json.load(f)
        age = (datetime.now(timezone.utc) -
               datetime.fromisoformat(data["saved_at"])).total_seconds() / 86400
        if age <= BASELINE_MAX_AGE:
            sensor.set_baseline(data["eco2_baseline"], data["tvoc_baseline"])
            print(f"Baseline loaded (age {age:.1f}d)", flush=True)
    except Exception as exc:
        print(f"Baseline load error: {exc}", file=sys.stderr, flush=True)


def _save_baseline(sensor) -> None:
    try:
        eco2b, tvocb = sensor.get_baseline()
        if eco2b == 0 and tvocb == 0:
            return
        tmp = BASELINE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"eco2_baseline": eco2b, "tvoc_baseline": tvocb,
                       "saved_at": datetime.now(timezone.utc).isoformat()}, f, indent=2)
        os.replace(tmp, BASELINE_FILE)  # atomic on POSIX — never a partial file
        print(f"Baseline saved: eCO2={eco2b} TVOC={tvocb}", flush=True)
    except Exception as exc:
        print(f"Baseline save error: {exc}", file=sys.stderr, flush=True)

# ── ntfy ──────────────────────────────────────────────────────────────────────
_LEVEL_LABELS = ["Good", "Moderate", "Poor", "Very Poor"]

def _tvoc_level(v: int) -> int:
    return 3 if v >= 5500 else 2 if v >= 2200 else 1 if v >= 660 else 0

def _eco2_level(v: int) -> int:
    return 3 if v >= 2000 else 2 if v >= 1500 else 1 if v >= 1000 else 0

def _ntfy_post(title: str, body: str, priority: str, tags: str) -> None:
    if not cfg.ntfy_url:
        return
    try:
        urllib.request.urlopen(
            urllib.request.Request(cfg.ntfy_url, data=body.encode(),
                headers={"Title": title, "Priority": priority, "Tags": tags},
                method="POST"), timeout=5)
        print(f"ntfy: {title}", flush=True)
    except Exception as exc:
        print(f"ntfy error: {exc}", file=sys.stderr, flush=True)

def _check_alerts(eco2: int, tvoc: int) -> None:
    tl, el = _tvoc_level(tvoc), _eco2_level(eco2)
    now = time.monotonic()
    worsened = tl > _ntfy_state["tvoc_lvl"] or el > _ntfy_state["eco2_lvl"]
    cooldown_expired = (now - _ntfy_state["last_sent"]) >= cfg.ntfy_cooldown
    if worsened or ((tl >= 2 or el >= 2) and cooldown_expired):
        msgs, worst = [], max(tl, el)
        if tl >= 2: msgs.append(f"TVOC {_LEVEL_LABELS[tl]}: {tvoc} ppb")
        if el >= 2: msgs.append(f"eCO₂ {_LEVEL_LABELS[el]}: {eco2} ppm")
        if msgs:
            _ntfy_post("⚠️ VOC Alert — SLV Makerspace",
                       "\n".join(msgs) + "\n\nVentilate the space.",
                       "urgent" if worst >= 3 else "high",
                       "rotating_light" if worst >= 3 else "warning")
            _ntfy_state["last_sent"] = now
    if tl < 2 and el < 2 and (_ntfy_state["tvoc_lvl"] >= 2 or _ntfy_state["eco2_lvl"] >= 2):
        _ntfy_post("✅ Air Quality Cleared — SLV Makerspace",
                   f"TVOC {tvoc} ppb, eCO₂ {eco2} ppm — back to normal.",
                   "default", "white_check_mark")
    _ntfy_state["tvoc_lvl"], _ntfy_state["eco2_lvl"] = tl, el

# ── CSV logging ───────────────────────────────────────────────────────────────
def _ensure_log() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not LOG_FILE.exists():
        with open(LOG_FILE, "w", newline="") as f:
            csv.writer(f).writerow(["timestamp", "eco2_ppm", "tvoc_ppb"])

def _append_csv(eco2: int, tvoc: int) -> None:
    ts = datetime.now(TZ).isoformat(timespec="seconds")
    with open(LOG_FILE, "a", newline="") as f:
        csv.writer(f).writerow([ts, eco2, tvoc])

# ── Google Sheets ─────────────────────────────────────────────────────────────
_gsheet_ws = None

def _get_ws():
    global _gsheet_ws
    if _gsheet_ws is None:
        try:
            import gspread
        except ImportError:
            raise RuntimeError(
                "gspread not installed — run: .venv/bin/pip install gspread google-auth")
        gc = gspread.service_account(filename=cfg.gsheet_credentials)
        _gsheet_ws = gc.open_by_key(cfg.gsheet_id).worksheet(cfg.gsheet_worksheet)
    return _gsheet_ws

def _gsheet_append(eco2: int, tvoc: int) -> None:
    global _gsheet_ws
    if not (cfg.gsheet_write and cfg.gsheet_id and cfg.gsheet_credentials):
        return
    ts = datetime.now(TZ).isoformat(timespec="seconds")
    for attempt in range(2):
        try:
            _get_ws().append_row([ts, eco2, tvoc], value_input_option="USER_ENTERED")
            return
        except Exception as exc:
            print(f"GSheet write error (attempt {attempt+1}): {exc}",
                  file=sys.stderr, flush=True)
            _gsheet_ws = None

def _read_gsheet_since(cutoff: float) -> list[dict]:
    global _gsheet_ws
    for attempt in range(2):
        try:
            rows = []
            for row in _get_ws().get_all_records():
                try:
                    epoch = datetime.fromisoformat(row["timestamp"]).timestamp()
                    if epoch >= cutoff:
                        rows.append({"t": int(epoch),
                                     "eco2": int(row["eco2_ppm"]),
                                     "tvoc": int(row["tvoc_ppb"])})
                except (ValueError, KeyError):
                    continue
            return rows
        except Exception as exc:
            print(f"GSheet read error (attempt {attempt+1}): {exc}",
                  file=sys.stderr, flush=True)
            _gsheet_ws = None
    return []

# ── Data source dispatch ──────────────────────────────────────────────────────
def _read_csv_since(cutoff: float) -> list[dict]:
    rows: list[dict] = []
    if not LOG_FILE.exists():
        return rows
    with open(LOG_FILE, newline="") as f:
        for row in csv.DictReader(f):
            try:
                epoch = datetime.fromisoformat(row["timestamp"]).timestamp()
                if epoch >= cutoff:
                    rows.append({"t": int(epoch),
                                 "eco2": int(row["eco2_ppm"]),
                                 "tvoc": int(row["tvoc_ppb"])})
            except (ValueError, KeyError):
                continue
    return rows

def _read_since(cutoff: float) -> list[dict]:
    if cfg.gsheet_read and cfg.gsheet_id:
        return _read_gsheet_since(cutoff)
    return _read_csv_since(cutoff)

def _process_28d(rows: list[dict]) -> list[dict]:
    from collections import defaultdict
    daily_eco2: dict = defaultdict(list)
    daily_tvoc: dict = defaultdict(list)
    for r in rows:
        day = r["t"] // 86400 * 86400
        daily_eco2[day].append(r["eco2"])
        daily_tvoc[day].append(r["tvoc"])
    hourly: dict = defaultdict(list)
    for r in rows:
        hourly[r["t"] // 3600 * 3600].append(r)
    result = []
    for ts in sorted(hourly):
        b = hourly[ts]
        day = ts // 86400 * 86400
        result.append({
            "t":        ts,
            "eco2":     round(sum(r["eco2"] for r in b) / len(b)),
            "tvoc":     round(sum(r["tvoc"] for r in b) / len(b)),
            "eco2_dmin": min(daily_eco2[day]),
            "eco2_dmax": max(daily_eco2[day]),
            "tvoc_dmin": min(daily_tvoc[day]),
            "tvoc_dmax": max(daily_tvoc[day]),
        })
    return result

# ── Sensor loop (normal mode) ─────────────────────────────────────────────────
def sensor_loop() -> None:
    from sgp30 import SGP30
    _ensure_log()
    while True:
        try:
            with SGP30() as sensor:
                sensor.iaq_init()
                _load_baseline(sensor)
                with _lock: _state["phase"] = "warmup"
                for _ in range(WARMUP_SECONDS):
                    sensor.measure_iaq(); time.sleep(1)
                with _lock: _state["phase"] = "running"
                print("Sensor running.", flush=True)
                last_baseline = last_log = time.monotonic()
                last_log = 0.0
                while True:
                    eco2, tvoc = sensor.measure_iaq()
                    ts = datetime.now(TZ).isoformat(timespec="seconds")
                    with _lock:
                        _state.update({"eco2": eco2, "tvoc": tvoc, "ts": ts})
                        _history.append({"t": int(time.time()),
                                         "eco2": eco2, "tvoc": tvoc})
                    _check_alerts(eco2, tvoc)
                    now = time.monotonic()
                    if now - last_log >= LOG_INTERVAL:
                        _append_csv(eco2, tvoc)
                        _gsheet_append(eco2, tvoc)
                        last_log = now
                    if now - last_baseline >= 3600:
                        _save_baseline(sensor); last_baseline = now
                    time.sleep(1)
        except Exception as exc:
            print(f"Sensor error: {exc} — retry in 5 s", file=sys.stderr, flush=True)
            with _lock: _state["phase"] = "error"
            time.sleep(5)

# ── Google Sheets read loop (dashboard mode — no sensor) ──────────────────────
def gsheet_read_loop() -> None:
    """Poll the Sheet every 60 s to populate the current-reading cards."""
    print("Dashboard mode: reading from Google Sheets.", flush=True)
    with _lock: _state["phase"] = "running"
    while True:
        try:
            rows = _read_gsheet_since(time.time() - 7200)
            if rows:
                r = rows[-1]
                ts = datetime.fromtimestamp(r["t"], TZ).isoformat(timespec="seconds")
                with _lock:
                    _state.update({"eco2": r["eco2"], "tvoc": r["tvoc"],
                                   "ts": ts, "phase": "running"})
        except Exception as exc:
            print(f"GSheet poll error: {exc}", file=sys.stderr, flush=True)
            with _lock: _state["phase"] = "error"
        time.sleep(60)

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/api/data")
def api_data():
    with _lock:
        return jsonify({"current": dict(_state),
                        "history": list(_history),
                        "mode": "gsheet_read" if cfg.gsheet_read else "sensor"})

@app.route("/api/history")
def api_history():
    rng = request.args.get("range", "24h")
    now = time.time()
    if rng == "24h":
        rows = _read_since(now - 86400)
    elif rng == "28d":
        rows = _process_28d(_read_since(now - 28 * 86400))
    else:
        return jsonify({"error": "invalid range"}), 400
    return jsonify({"data": rows})

@app.route("/api/download")
def api_download():
    if not LOG_FILE.exists():
        return "No data recorded yet.", 404
    return send_file(LOG_FILE, as_attachment=True,
                     download_name="voc_log.csv", mimetype="text/csv")

@app.route("/")
def index():
    return _HTML

# ── Embedded dashboard ────────────────────────────────────────────────────────
_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Air Quality Monitor</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:#f1f5f9;color:#1e293b;padding:16px;min-height:100vh}
.wrap{max-width:780px;margin:0 auto}
header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:4px}
h1{font-size:1.3rem;font-weight:700;color:#0f172a}
.sub{font-size:.76rem;color:#64748b;margin-bottom:14px}
.badge{font-size:.73rem;font-weight:600;padding:4px 11px;border-radius:999px;
  white-space:nowrap;margin-top:2px}
.ph-starting{background:#e2e8f0;color:#64748b}
.ph-warmup{background:#fef9c3;color:#854d0e}
.ph-running{background:#dcfce7;color:#166534}
.ph-error{background:#fee2e2;color:#991b1b}
.alert{border-radius:10px;padding:10px 14px;font-size:.82rem;font-weight:500;
  margin-bottom:14px;display:none;line-height:1.5}
.alert-warn{background:#fff7ed;border:1px solid #fed7aa;color:#9a3412}
.alert-crit{background:#fee2e2;border:1px solid #fca5a5;color:#7f1d1d}
.alert-ok  {background:#f0fdf4;border:1px solid #86efac;color:#166534}
.cards{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:16px}
@media(max-width:440px){.cards{grid-template-columns:1fr}}
.card{background:#fff;border-radius:12px;padding:18px;
  box-shadow:0 1px 3px rgba(0,0,0,.08)}
.clabel{font-size:.68rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.07em;color:#64748b;margin-bottom:7px}
.cval{font-size:2.7rem;font-weight:700;line-height:1;color:#0f172a;
  margin-bottom:3px;transition:color .4s}
.cunit{font-size:.76rem;color:#94a3b8;margin-bottom:11px}
.cbadge{display:inline-block;padding:3px 10px;border-radius:999px;
  font-size:.73rem;font-weight:600;margin-bottom:5px}
.cdesc{font-size:.73rem;color:#64748b;min-height:1em}
.chart-panel{background:#fff;border-radius:12px;padding:18px;
  box-shadow:0 1px 3px rgba(0,0,0,.08);margin-bottom:14px}
.shd{font-size:.68rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.08em;color:#94a3b8;display:flex;align-items:center;
  gap:8px;margin-bottom:12px}
.shd::after{content:'';flex:1;height:1px;background:#e2e8f0}
.crow{margin-bottom:12px}
.crow:last-child{margin-bottom:0}
.clabel2{font-size:.7rem;color:#94a3b8;margin-bottom:3px;
  display:flex;justify-content:space-between}
.minmax{font-size:.68rem;color:#cbd5e1}
canvas{width:100%;display:block;border-radius:4px;cursor:crosshair}
.dl-btn{display:inline-block;background:#f1f5f9;color:#334155;
  border:1px solid #e2e8f0;border-radius:8px;padding:8px 16px;
  font-size:.8rem;font-weight:600;text-decoration:none;margin-bottom:16px;
  transition:background .15s}
.dl-btn:hover{background:#e2e8f0}
.info{background:#fff;border-radius:12px;padding:18px;
  box-shadow:0 1px 3px rgba(0,0,0,.08)}
.info h2{font-size:.95rem;font-weight:700;color:#0f172a;margin-bottom:12px}
.info h3{font-size:.85rem;font-weight:600;color:#334155;margin:16px 0 6px}
.info p{font-size:.8rem;color:#475569;line-height:1.65;margin-bottom:8px}
table{width:100%;border-collapse:collapse;font-size:.76rem;margin-bottom:4px}
th{text-align:left;padding:5px 8px;background:#f8fafc;color:#64748b;
  font-weight:600;border-bottom:1px solid #e2e8f0}
td{padding:5px 8px;border-bottom:1px solid #f1f5f9;color:#374151}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;
  margin-right:5px;vertical-align:middle}
.note{background:#f0f9ff;border-left:3px solid #38bdf8;padding:9px 13px;
  border-radius:0 6px 6px 0;font-size:.77rem;color:#0c4a6e;
  margin-top:12px;line-height:1.6}
#tt{position:fixed;background:rgba(15,23,42,.93);color:#f1f5f9;
  padding:7px 11px;border-radius:7px;font-size:.75rem;pointer-events:none;
  display:none;z-index:999;line-height:1.65;white-space:nowrap;
  box-shadow:0 2px 8px rgba(0,0,0,.3)}
</style>
</head>
<body>
<div id="tt"></div>
<div class="wrap">

<header>
  <div><h1>Air Quality Monitor</h1></div>
  <span class="badge ph-starting" id="badge">Starting</span>
</header>
<div class="sub" id="sub">Connecting&hellip;</div>

<div class="alert" id="alert-banner"></div>

<div class="cards">
  <div class="card">
    <div class="clabel">TVOC &mdash; Total VOC</div>
    <div class="cval" id="tvoc-val">&mdash;</div>
    <div class="cunit">parts per billion (ppb)</div>
    <span class="cbadge" id="tvoc-badge" style="background:#e2e8f0;color:#64748b">&mdash;</span>
    <div class="cdesc" id="tvoc-desc"></div>
  </div>
  <div class="card">
    <div class="clabel">eCO&sup2; &mdash; Equiv. CO&sup2;</div>
    <div class="cval" id="eco2-val">&mdash;</div>
    <div class="cunit">parts per million (ppm)</div>
    <span class="cbadge" id="eco2-badge" style="background:#e2e8f0;color:#64748b">&mdash;</span>
    <div class="cdesc" id="eco2-desc"></div>
  </div>
</div>

<div class="chart-panel" id="live-panel">
  <div class="shd">Live &mdash; Last 5 Minutes</div>
  <div class="crow">
    <div class="clabel2">TVOC (ppb)<span class="minmax" id="mm-tvoc-live"></span></div>
    <canvas id="tvoc-live" style="height:60px"></canvas>
  </div>
  <div class="crow">
    <div class="clabel2">eCO&sup2; (ppm)<span class="minmax" id="mm-eco2-live"></span></div>
    <canvas id="eco2-live" style="height:60px"></canvas>
  </div>
</div>

<div class="chart-panel">
  <div class="shd">Last 24 Hours</div>
  <div class="crow">
    <div class="clabel2">TVOC (ppb)<span class="minmax" id="mm-tvoc-24h"></span></div>
    <canvas id="tvoc-24h" style="height:90px"></canvas>
  </div>
  <div class="crow">
    <div class="clabel2">eCO&sup2; (ppm)<span class="minmax" id="mm-eco2-24h"></span></div>
    <canvas id="eco2-24h" style="height:90px"></canvas>
  </div>
</div>

<div class="chart-panel">
  <div class="shd">Last 28 Days &mdash; Hourly Avg &amp; Daily Range</div>
  <div class="crow">
    <div class="clabel2">TVOC (ppb)<span class="minmax" id="mm-tvoc-28d"></span></div>
    <canvas id="tvoc-28d" style="height:90px"></canvas>
  </div>
  <div class="crow">
    <div class="clabel2">eCO&sup2; (ppm)<span class="minmax" id="mm-eco2-28d"></span></div>
    <canvas id="eco2-28d" style="height:90px"></canvas>
  </div>
</div>

<a href="/api/download" class="dl-btn">&#8615; Download CSV</a>

<div class="info">
  <h2>What do these numbers mean?</h2>
  <h3>TVOC &mdash; Total Volatile Organic Compounds</h3>
  <p>VOCs are gases from cleaning products, adhesives, paints, 3D printer fumes,
  soldering, resin, laser cutting, and people. In a makerspace these spike during
  active work sessions. Clean outdoor air reads roughly 50&ndash;150&nbsp;ppb.</p>
  <table>
    <tr><th>Rating</th><th>TVOC</th><th>Meaning</th></tr>
    <tr><td><span class="dot" style="background:#10b981"></span>Excellent</td><td>0&ndash;220 ppb</td><td>Clean air</td></tr>
    <tr><td><span class="dot" style="background:#22c55e"></span>Good</td><td>220&ndash;660 ppb</td><td>Acceptable</td></tr>
    <tr><td><span class="dot" style="background:#f59e0b"></span>Moderate</td><td>660&ndash;2200 ppb</td><td>Open windows or run ventilation</td></tr>
    <tr><td><span class="dot" style="background:#f97316"></span>Poor</td><td>2200&ndash;5500 ppb</td><td>Ventilate now &mdash; ntfy alert sent</td></tr>
    <tr><td><span class="dot" style="background:#ef4444"></span>Very Poor</td><td>&gt;5500 ppb</td><td>Stop work, ventilate immediately &mdash; urgent alert</td></tr>
  </table>
  <h3>eCO&sup2; &mdash; Equivalent CO&sup2;</h3>
  <p>Estimated from VOC readings (not a true CO&sup2; sensor). Tracks real CO&sup2;
  well in normal occupancy conditions and is a good proxy for ventilation quality.
  Outdoor air &asymp; 400&ndash;420&nbsp;ppm.</p>
  <table>
    <tr><th>Rating</th><th>eCO&sup2;</th><th>Meaning</th></tr>
    <tr><td><span class="dot" style="background:#10b981"></span>Excellent</td><td>400&ndash;600 ppm</td><td>Well ventilated</td></tr>
    <tr><td><span class="dot" style="background:#22c55e"></span>Good</td><td>600&ndash;1000 ppm</td><td>Acceptable</td></tr>
    <tr><td><span class="dot" style="background:#f59e0b"></span>Moderate</td><td>1000&ndash;1500 ppm</td><td>Getting stuffy &mdash; open a window</td></tr>
    <tr><td><span class="dot" style="background:#f97316"></span>Poor</td><td>1500&ndash;2000 ppm</td><td>Poor ventilation &mdash; act soon</td></tr>
    <tr><td><span class="dot" style="background:#ef4444"></span>Very Poor</td><td>&gt;2000 ppm</td><td>Very stuffy &mdash; urgent alert</td></tr>
  </table>
  <div class="note">
    <strong>28-day chart:</strong> shaded band = full daily min&ndash;max from
    5-minute readings; line = hourly average. A break in the line means the
    sensor was offline.<br><br>
    <strong>Calibration:</strong> SGP30 warms up for 15 s and improves over
    12 hours. Baseline saved hourly and restored on restart.
  </div>
</div>

</div>
<script>
const TVOC_L=[
  {max:220,  color:'#10b981',bg:'#d1fae5',label:'Excellent',desc:'Clean air'},
  {max:660,  color:'#22c55e',bg:'#dcfce7',label:'Good',     desc:'Acceptable indoor air'},
  {max:2200, color:'#f59e0b',bg:'#fef9c3',label:'Moderate', desc:'Some pollutants — run ventilation'},
  {max:5500, color:'#f97316',bg:'#ffedd5',label:'Poor',     desc:'High pollution — ventilate now'},
  {max:1e9,  color:'#ef4444',bg:'#fee2e2',label:'Very Poor',desc:'Harmful — stop work & ventilate'},
];
const ECO2_L=[
  {max:600,  color:'#10b981',bg:'#d1fae5',label:'Excellent',desc:'Well ventilated'},
  {max:1000, color:'#22c55e',bg:'#dcfce7',label:'Good',     desc:'Acceptable'},
  {max:1500, color:'#f59e0b',bg:'#fef9c3',label:'Moderate', desc:'Getting stuffy — open a window'},
  {max:2000, color:'#f97316',bg:'#ffedd5',label:'Poor',     desc:'Poor ventilation — act soon'},
  {max:1e9,  color:'#ef4444',bg:'#fee2e2',label:'Very Poor',desc:'Very stuffy — ventilate now'},
];
function lvl(v,T){return T.find(l=>v<l.max)||T[T.length-1];}

// ── Tooltip ───────────────────────────────────────────────────────────────────
const tt=document.getElementById('tt');
function showTT(html,cx,cy){
  tt.innerHTML=html; tt.style.display='block';
  const tw=tt.offsetWidth,th=tt.offsetHeight,wx=window.innerWidth,wy=window.innerHeight;
  let x=cx+14,y=cy-th/2;
  if(x+tw>wx-8)x=cx-tw-14; if(y<8)y=8; if(y+th>wy-8)y=wy-th-8;
  tt.style.left=x+'px'; tt.style.top=y+'px';
}
function hideTT(){tt.style.display='none';}

// ── Chart store ───────────────────────────────────────────────────────────────
const _cs={};

function drawChart(id,data,key,color,gapSec,range,mmId,envMin,envMax){
  const canvas=document.getElementById(id); if(!canvas) return;
  const vals=data.map(d=>d[key]);
  if(mmId&&vals.length){
    const lo=Math.min(...vals),hi=Math.max(...vals);
    document.getElementById(mmId).textContent=`min ${lo}  max ${hi}`;
  }
  if(vals.length<2) return;
  const dpr=window.devicePixelRatio||1;
  const rect=canvas.getBoundingClientRect();
  canvas.width=rect.width*dpr; canvas.height=rect.height*dpr;
  const ctx=canvas.getContext('2d'); ctx.scale(dpr,dpr);
  const W=rect.width,H=rect.height,ML=46,MR=6,MT=8,MB=20;
  const PW=W-ML-MR,PH=H-MT-MB;
  const envLo=envMin?Math.min(...data.map(d=>d[envMin])):Infinity;
  const envHi=envMax?Math.max(...data.map(d=>d[envMax])):-Infinity;
  const lo=Math.min(Math.min(...vals),envLo),hi=Math.max(Math.max(...vals),envHi);
  const span=(hi-lo)||1;
  const px=i=>ML+(i/(vals.length-1))*PW;
  const py=v=>MT+PH*(0.94-0.88*(v-lo)/span);
  _cs[id]={data,key,range,ML,PW,lo,span,MT,PH,envMin,envMax,color};

  // Background + axes
  ctx.fillStyle='#f8fafc'; ctx.fillRect(ML,MT,PW,PH);
  ctx.strokeStyle='#e2e8f0'; ctx.lineWidth=1;
  ctx.beginPath(); ctx.moveTo(ML,MT); ctx.lineTo(ML,MT+PH);
  ctx.moveTo(ML,MT+PH); ctx.lineTo(ML+PW,MT+PH); ctx.stroke();

  // Y labels
  ctx.font='10px system-ui,sans-serif'; ctx.fillStyle='#94a3b8'; ctx.textAlign='right';
  ctx.textBaseline='top';    ctx.fillText(Math.round(hi),        ML-4,MT+1);
  ctx.textBaseline='middle'; ctx.fillStyle='#cbd5e1';
                              ctx.fillText(Math.round((lo+hi)/2), ML-4,MT+PH*0.47);
  ctx.textBaseline='bottom'; ctx.fillStyle='#94a3b8';
                              ctx.fillText(Math.round(lo),        ML-4,MT+PH-1);

  // X labels
  const n=vals.length, step=Math.max(1,Math.floor(n/5));
  const fmtX=range==='24h'
    ?t=>{const d=new Date(t*1000);return String(d.getHours()).padStart(2,'0')+':00';}
    :range==='28d'
    ?t=>new Date(t*1000).toLocaleDateString('en-US',{month:'short',day:'numeric'})
    :t=>{const d=new Date(t*1000);return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');};
  ctx.textAlign='center'; ctx.textBaseline='top'; ctx.fillStyle='#94a3b8';
  for(let i=0;i<n;i+=step){
    const x=px(i);
    ctx.strokeStyle='#e2e8f0'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(x,MT+PH); ctx.lineTo(x,MT+PH+4); ctx.stroke();
    ctx.fillText(fmtX(data[i].t),x,MT+PH+5);
  }

  // Daily envelope
  if(envMin&&envMax){
    ctx.globalAlpha=0.15; ctx.fillStyle=color; ctx.beginPath();
    for(let i=0;i<n;i++){const x=px(i),y=py(data[i][envMax]||vals[i]); i===0?ctx.moveTo(x,y):ctx.lineTo(x,y);}
    for(let i=n-1;i>=0;i--)ctx.lineTo(px(i),py(data[i][envMin]||vals[i]));
    ctx.closePath(); ctx.fill(); ctx.globalAlpha=1;
  }

  // Line with gap detection
  ctx.beginPath(); ctx.strokeStyle=color; ctx.lineWidth=1.8; ctx.lineJoin='round';
  for(let i=0;i<n;i++){
    const gap=i>0&&(data[i].t-data[i-1].t)>gapSec;
    gap||i===0?ctx.moveTo(px(i),py(vals[i])):ctx.lineTo(px(i),py(vals[i]));
  }
  ctx.stroke();

  // Area fill
  ctx.globalAlpha=0.10; ctx.fillStyle=color; let open=false; ctx.beginPath();
  for(let i=0;i<n;i++){
    const x=px(i),y=py(vals[i]),gap=i>0&&(data[i].t-data[i-1].t)>gapSec;
    if(gap||i===0){if(open){ctx.lineTo(px(i-1),MT+PH);ctx.closePath();ctx.fill();ctx.beginPath();}ctx.moveTo(x,MT+PH);ctx.lineTo(x,y);open=true;}else ctx.lineTo(x,y);
  }
  if(open){ctx.lineTo(px(n-1),MT+PH);ctx.closePath();ctx.fill();}
  ctx.globalAlpha=1;

  // Terminal dot
  ctx.beginPath(); ctx.arc(px(n-1),py(vals[n-1]),3.5,0,Math.PI*2);
  ctx.fillStyle=color; ctx.fill();
}

// ── Tooltip hit-testing ───────────────────────────────────────────────────────
function chartHover(e,id){
  const s=_cs[id]; if(!s||s.data.length<2) return;
  const rect=document.getElementById(id).getBoundingClientRect();
  const cx=e.touches?e.touches[0].clientX:e.clientX;
  const cy=e.touches?e.touches[0].clientY:e.clientY;
  const frac=Math.max(0,Math.min(1,(cx-rect.left-s.ML)/s.PW));
  const idx=Math.round(frac*(s.data.length-1));
  const pt=s.data[Math.max(0,Math.min(s.data.length-1,idx))]; if(!pt) return;
  const val=pt[s.key], unit=s.key==='tvoc'?'ppb':'ppm';
  const l=lvl(val,s.key==='tvoc'?TVOC_L:ECO2_L);
  const d=new Date(pt.t*1000);
  const ts=s.range==='28d'?d.toLocaleDateString('en-US',{weekday:'short',month:'short',day:'numeric'})
    :s.range==='24h'?d.toLocaleString('en-US',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})
    :d.toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
  let html=`<b>${ts}</b><br>${val} ${unit} &mdash; <span style="color:${l.color}">${l.label}</span>`;
  if(s.envMin&&pt[s.envMin]!==undefined)
    html+=`<br><span style="color:#94a3b8;font-size:.7rem">Day ${pt[s.envMin]}–${pt[s.envMax]} ${unit}</span>`;
  showTT(html,cx,cy);
}
function attachTT(id){
  const c=document.getElementById(id); if(!c||c._tt) return; c._tt=true;
  c.addEventListener('mousemove',e=>chartHover(e,id));
  c.addEventListener('mouseleave',hideTT);
  c.addEventListener('touchmove',e=>{e.preventDefault();chartHover(e,id);},{passive:false});
  c.addEventListener('touchend',hideTT);
}
['tvoc-live','eco2-live','tvoc-24h','eco2-24h','tvoc-28d','eco2-28d'].forEach(attachTT);

// ── Alert banner ──────────────────────────────────────────────────────────────
let _prevBad=false;
function updateAlert(tvoc,eco2){
  const tl=lvl(tvoc,TVOC_L),el=lvl(eco2,ECO2_L);
  const b=document.getElementById('alert-banner'), msgs=[];
  if(tvoc>=2200)msgs.push(`TVOC ${tl.label}: ${tvoc} ppb — ${tl.desc}.`);
  if(eco2>=1500)msgs.push(`eCO₂ ${el.label}: ${eco2} ppm — ${el.desc}.`);
  if(msgs.length){
    b.textContent=msgs.join(' ');
    b.className='alert '+(tvoc>=5500||eco2>=2000?'alert-crit':'alert-warn');
    b.style.display='block'; _prevBad=true;
  } else if(_prevBad){
    b.textContent='Air quality back to normal.';
    b.className='alert alert-ok'; b.style.display='block'; _prevBad=false;
  } else { b.style.display='none'; }
}

// ── Phase badge ───────────────────────────────────────────────────────────────
const PHASE={
  starting:['Starting…','ph-starting'],warmup:['Warming up…','ph-warmup'],
  running:['Running','ph-running'],error:['Sensor error','ph-error'],
};

// ── Live poll ─────────────────────────────────────────────────────────────────
let _gsheetRead=false;
async function fetchLive(){
  try{
    const {current:c,history:h,mode}=await(await fetch('/api/data')).json();

    // Hide live panel in Google Sheets read mode
    if(mode==='gsheet_read'){
      _gsheetRead=true;
      document.getElementById('live-panel').style.display='none';
      document.getElementById('sub').textContent='Showing data from Google Sheets';
    }

    const[pl,pc]=PHASE[c.phase]||PHASE.starting;
    const b=document.getElementById('badge'); b.textContent=pl; b.className='badge '+pc;
    if(c.ts&&!_gsheetRead)
      document.getElementById('sub').textContent='SGP30 · Updated '+c.ts.slice(11,19);
    if(c.tvoc!==null){
      const l=lvl(c.tvoc,TVOC_L);
      const v=document.getElementById('tvoc-val'); v.textContent=c.tvoc; v.style.color=l.color;
      const bg=document.getElementById('tvoc-badge');
      bg.textContent=l.label; bg.style.background=l.bg; bg.style.color=l.color;
      document.getElementById('tvoc-desc').textContent=l.desc;
    }
    if(c.eco2!==null){
      const l=lvl(c.eco2,ECO2_L);
      const v=document.getElementById('eco2-val'); v.textContent=c.eco2; v.style.color=l.color;
      const bg=document.getElementById('eco2-badge');
      bg.textContent=l.label; bg.style.background=l.bg; bg.style.color=l.color;
      document.getElementById('eco2-desc').textContent=l.desc;
    }
    if(c.tvoc!==null&&c.eco2!==null) updateAlert(c.tvoc,c.eco2);
    if(!_gsheetRead&&h.length>1){
      const tc=c.tvoc!==null?lvl(c.tvoc,TVOC_L).color:'#64748b';
      const ec=c.eco2!==null?lvl(c.eco2,ECO2_L).color:'#64748b';
      drawChart('tvoc-live',h,'tvoc',tc,120,'5m','mm-tvoc-live',null,null);
      drawChart('eco2-live',h,'eco2',ec,120,'5m','mm-eco2-live',null,null);
    }
  }catch(_){
    const b=document.getElementById('badge');
    b.textContent='Connection error'; b.className='badge ph-error';
  }
}

// ── Historical charts ─────────────────────────────────────────────────────────
async function fetchHistory(range){
  try{
    const {data}=await(await fetch('/api/history?range='+range)).json();
    if(!data||data.length<2) return;
    const last=data[data.length-1];
    const tc=lvl(last.tvoc,TVOC_L).color,ec=lvl(last.eco2,ECO2_L).color;
    const gapSec=range==='28d'?10800:600;
    const[tMin,tMax,eMin,eMax]=range==='28d'
      ?['tvoc_dmin','tvoc_dmax','eco2_dmin','eco2_dmax']:[null,null,null,null];
    drawChart(`tvoc-${range}`,data,'tvoc',tc,gapSec,range,`mm-tvoc-${range}`,tMin,tMax);
    drawChart(`eco2-${range}`,data,'eco2',ec,gapSec,range,`mm-eco2-${range}`,eMin,eMax);
  }catch(e){console.warn('history',range,e);}
}

function refreshAll(){fetchHistory('24h');fetchHistory('28d');}
fetchLive(); refreshAll();
setInterval(fetchLive,2000); setInterval(refreshAll,300000);
window.addEventListener('resize',()=>{fetchLive();refreshAll();});
</script>
</body>
</html>"""


if __name__ == "__main__":
    if cfg.ntfy_url:
        print(f"ntfy alerts → {cfg.ntfy_url}", flush=True)
    if cfg.gsheet_write:
        print(f"GSheet write → {cfg.gsheet_id} / {cfg.gsheet_worksheet}", flush=True)
    if cfg.gsheet_read:
        threading.Thread(target=gsheet_read_loop, daemon=True).start()
    else:
        threading.Thread(target=sensor_loop, daemon=True).start()
    print(f"Dashboard: http://0.0.0.0:{cfg.port}", flush=True)
    app.run(host="0.0.0.0", port=cfg.port, debug=False, use_reloader=False)
