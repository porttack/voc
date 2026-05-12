#!/usr/bin/env python3
"""SGP30 VOC web dashboard — live air quality monitor on port 8080."""

import collections
import csv
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, send_file

from sgp30 import SGP30

# ── Config ────────────────────────────────────────────────────────────────────
BASELINE_FILE = "baseline.json"
BASELINE_MAX_AGE_DAYS = 7
WARMUP_SECONDS = 15
MAX_LIVE_HISTORY = 300      # 5 min at 1 Hz
LOG_DIR = Path.home() / ".local" / "voc"
LOG_FILE = LOG_DIR / "voc.csv"
LOG_INTERVAL = 300          # seconds between CSV rows
PORT = 8080
TZ = ZoneInfo("America/Los_Angeles")

# ── Shared state ──────────────────────────────────────────────────────────────
app = Flask(__name__)
_lock = threading.Lock()
_history: collections.deque = collections.deque(maxlen=MAX_LIVE_HISTORY)
_state: dict = {"eco2": None, "tvoc": None, "ts": None, "phase": "starting"}


# ── Baseline ──────────────────────────────────────────────────────────────────
def _load_baseline(sensor: SGP30) -> None:
    if not os.path.exists(BASELINE_FILE):
        return
    try:
        with open(BASELINE_FILE) as f:
            data = json.load(f)
        saved_at = datetime.fromisoformat(data["saved_at"])
        age = (datetime.now(timezone.utc) - saved_at).total_seconds() / 86400
        if age <= BASELINE_MAX_AGE_DAYS:
            sensor.set_baseline(data["eco2_baseline"], data["tvoc_baseline"])
            print(f"Loaded baseline (age {age:.1f}d)", flush=True)
    except Exception as exc:
        print(f"Baseline load error: {exc}", file=sys.stderr, flush=True)


def _save_baseline(sensor: SGP30) -> None:
    try:
        eco2b, tvocb = sensor.get_baseline()
        if eco2b == 0 and tvocb == 0:
            return
        payload = {"eco2_baseline": eco2b, "tvoc_baseline": tvocb,
                   "saved_at": datetime.now(timezone.utc).isoformat()}
        with open(BASELINE_FILE, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"Baseline saved: eCO2={eco2b}, TVOC={tvocb}", flush=True)
    except Exception as exc:
        print(f"Baseline save error: {exc}", file=sys.stderr, flush=True)


# ── CSV logging ───────────────────────────────────────────────────────────────
def _ensure_log() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not LOG_FILE.exists():
        with open(LOG_FILE, "w", newline="") as f:
            csv.writer(f).writerow(["timestamp", "eco2_ppm", "tvoc_ppb"])


def _append_log(eco2: int, tvoc: int) -> None:
    ts = datetime.now(TZ).isoformat(timespec="seconds")
    with open(LOG_FILE, "a", newline="") as f:
        csv.writer(f).writerow([ts, eco2, tvoc])


# ── Sensor loop ───────────────────────────────────────────────────────────────
def sensor_loop() -> None:
    _ensure_log()
    while True:
        try:
            with SGP30() as sensor:
                sensor.iaq_init()
                _load_baseline(sensor)

                with _lock:
                    _state["phase"] = "warmup"
                for _ in range(WARMUP_SECONDS):
                    sensor.measure_iaq()
                    time.sleep(1)

                with _lock:
                    _state["phase"] = "running"
                print("Running.", flush=True)

                last_baseline = time.monotonic()
                last_log = 0.0  # ensure first reading is logged immediately

                while True:
                    eco2, tvoc = sensor.measure_iaq()
                    ts = datetime.now(TZ).isoformat(timespec="seconds")
                    with _lock:
                        _state.update({"eco2": eco2, "tvoc": tvoc, "ts": ts})
                        _history.append({"t": int(time.time()),
                                         "eco2": eco2, "tvoc": tvoc})
                    now = time.monotonic()
                    if now - last_log >= LOG_INTERVAL:
                        _append_log(eco2, tvoc)
                        last_log = now
                    if now - last_baseline >= 3600:
                        _save_baseline(sensor)
                        last_baseline = now
                    time.sleep(1)

        except Exception as exc:
            print(f"Sensor error: {exc} — retrying in 5 s", file=sys.stderr, flush=True)
            with _lock:
                _state["phase"] = "error"
            time.sleep(5)


# ── History helpers ───────────────────────────────────────────────────────────
def _read_csv_since(cutoff_epoch: float) -> list[dict]:
    rows: list[dict] = []
    if not LOG_FILE.exists():
        return rows
    with open(LOG_FILE, newline="") as f:
        for row in csv.DictReader(f):
            try:
                epoch = datetime.fromisoformat(row["timestamp"]).timestamp()
                if epoch >= cutoff_epoch:
                    rows.append({"t": int(epoch),
                                 "eco2": int(row["eco2_ppm"]),
                                 "tvoc": int(row["tvoc_ppb"])})
            except (ValueError, KeyError):
                continue
    return rows


def _hourly_avg(rows: list[dict]) -> list[dict]:
    from collections import defaultdict
    buckets: dict = defaultdict(list)
    for r in rows:
        buckets[r["t"] // 3600 * 3600].append(r)
    return [{"t": ts,
             "eco2": round(sum(r["eco2"] for r in b) / len(b)),
             "tvoc": round(sum(r["tvoc"] for r in b) / len(b))}
            for ts, b in sorted(buckets.items())]


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/api/data")
def api_data():
    with _lock:
        return jsonify({"current": dict(_state), "history": list(_history)})


@app.route("/api/history")
def api_history():
    rng = request.args.get("range", "24h")
    now = time.time()
    if rng == "24h":
        rows = _read_csv_since(now - 86400)
    elif rng == "28d":
        rows = _hourly_avg(_read_csv_since(now - 28 * 86400))
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
.badge{font-size:.73rem;font-weight:600;padding:4px 11px;border-radius:999px;white-space:nowrap;margin-top:2px}
.ph-starting{background:#e2e8f0;color:#64748b}
.ph-warmup{background:#fef9c3;color:#854d0e}
.ph-running{background:#dcfce7;color:#166534}
.ph-error{background:#fee2e2;color:#991b1b}

.alert{background:#fff7ed;border:1px solid #fed7aa;color:#9a3412;border-radius:10px;
  padding:10px 14px;font-size:.82rem;font-weight:500;margin-bottom:14px;display:none;
  line-height:1.5}
.alert.bad{background:#fee2e2;border-color:#fca5a5;color:#991b1b}

.cards{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:16px}
@media(max-width:440px){.cards{grid-template-columns:1fr}}
.card{background:#fff;border-radius:12px;padding:18px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.clabel{font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;
  color:#64748b;margin-bottom:7px}
.cval{font-size:2.7rem;font-weight:700;line-height:1;color:#0f172a;margin-bottom:3px;
  transition:color .4s}
.cunit{font-size:.76rem;color:#94a3b8;margin-bottom:11px}
.cbadge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:.73rem;
  font-weight:600;margin-bottom:5px}
.cdesc{font-size:.73rem;color:#64748b;min-height:1em}

.chart-panel{background:#fff;border-radius:12px;padding:18px;
  box-shadow:0 1px 3px rgba(0,0,0,.08);margin-bottom:14px}
.section-hd{font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;
  color:#94a3b8;display:flex;align-items:center;gap:8px;margin-bottom:12px}
.section-hd::after{content:'';flex:1;height:1px;background:#e2e8f0}
.crow{margin-bottom:12px}
.crow:last-child{margin-bottom:0}
.clabel2{font-size:.7rem;color:#94a3b8;margin-bottom:3px;
  display:flex;justify-content:space-between}
.clabel2 .minmax{font-size:.68rem;color:#cbd5e1}
canvas{width:100%;display:block;border-radius:6px}

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
  border-radius:0 6px 6px 0;font-size:.77rem;color:#0c4a6e;margin-top:12px;line-height:1.6}
</style>
</head>
<body>
<div class="wrap">

<header>
  <div><h1>Air Quality Monitor</h1></div>
  <span class="badge ph-starting" id="badge">Starting</span>
</header>
<div class="sub" id="sub">Connecting to sensor&hellip;</div>

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

<div class="chart-panel">
  <div class="section-hd">Live &mdash; Last 5 Minutes</div>
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
  <div class="section-hd">Last 24 Hours</div>
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
  <div class="section-hd">Last 28 Days &mdash; Hourly Averages</div>
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
  <p>VOCs are gases emitted by cleaning products, paints, adhesives, furniture,
  cooking, and even people breathing. High levels cause headaches and irritation;
  long-term exposure at very high levels has more serious health effects.
  Clean outdoor air typically reads 50&ndash;150&nbsp;ppb.</p>
  <table>
    <tr><th>Rating</th><th>TVOC</th><th>Meaning</th></tr>
    <tr><td><span class="dot" style="background:#10b981"></span>Excellent</td><td>0&ndash;220 ppb</td><td>Clean air, typical outdoors</td></tr>
    <tr><td><span class="dot" style="background:#22c55e"></span>Good</td><td>220&ndash;660 ppb</td><td>Acceptable indoor air</td></tr>
    <tr><td><span class="dot" style="background:#f59e0b"></span>Moderate</td><td>660&ndash;2200 ppb</td><td>Some pollutants &mdash; open a window</td></tr>
    <tr><td><span class="dot" style="background:#f97316"></span>Poor</td><td>2200&ndash;5500 ppb</td><td>High pollution &mdash; ventilate now</td></tr>
    <tr><td><span class="dot" style="background:#ef4444"></span>Very Poor</td><td>&gt;5500 ppb</td><td>Harmful &mdash; ventilate immediately</td></tr>
  </table>

  <h3>eCO&sup2; &mdash; Equivalent CO&sup2;</h3>
  <p>The SGP30 <em>estimates</em> CO&sup2; from its VOC readings &mdash; it is not a
  dedicated CO&sup2; sensor. In typical indoor environments the estimate correlates
  well with actual CO&sup2; and is a practical proxy for ventilation quality.
  Outdoor air is ~400&ndash;420&nbsp;ppm; a poorly ventilated occupied room climbs
  past 1000&nbsp;ppm within an hour.</p>
  <table>
    <tr><th>Rating</th><th>eCO&sup2;</th><th>Meaning</th></tr>
    <tr><td><span class="dot" style="background:#10b981"></span>Excellent</td><td>400&ndash;600 ppm</td><td>Well-ventilated, fresh air</td></tr>
    <tr><td><span class="dot" style="background:#22c55e"></span>Good</td><td>600&ndash;1000 ppm</td><td>Acceptable ventilation</td></tr>
    <tr><td><span class="dot" style="background:#f59e0b"></span>Moderate</td><td>1000&ndash;1500 ppm</td><td>Getting stuffy &mdash; ventilate</td></tr>
    <tr><td><span class="dot" style="background:#f97316"></span>Poor</td><td>1500&ndash;2000 ppm</td><td>Poor ventilation &mdash; act soon</td></tr>
    <tr><td><span class="dot" style="background:#ef4444"></span>Very Poor</td><td>&gt;2000 ppm</td><td>Very stuffy &mdash; ventilate now</td></tr>
  </table>

  <div class="note">
    <strong>Calibration:</strong> The SGP30 warms up for 15 s after power-on and
    takes up to 12 hours to fully calibrate its baseline. Readings improve over
    time. The service saves a baseline automatically every hour and restores it
    on restart, so accuracy is preserved across reboots.
  </div>
</div>

</div><!-- /wrap -->
<script>
// ── Level tables ──────────────────────────────────────────────────────────────
const TVOC_L=[
  {max:220,  color:'#10b981',bg:'#d1fae5',label:'Excellent',desc:'Clean air — typical outdoor levels'},
  {max:660,  color:'#22c55e',bg:'#dcfce7',label:'Good',     desc:'Acceptable indoor air quality'},
  {max:2200, color:'#f59e0b',bg:'#fef9c3',label:'Moderate', desc:'Some pollutants present — ventilate'},
  {max:5500, color:'#f97316',bg:'#ffedd5',label:'Poor',     desc:'High pollution — open windows now'},
  {max:1e9,  color:'#ef4444',bg:'#fee2e2',label:'Very Poor',desc:'Harmful levels — ventilate immediately'},
];
const ECO2_L=[
  {max:600,  color:'#10b981',bg:'#d1fae5',label:'Excellent',desc:'Well-ventilated, fresh air'},
  {max:1000, color:'#22c55e',bg:'#dcfce7',label:'Good',     desc:'Acceptable ventilation'},
  {max:1500, color:'#f59e0b',bg:'#fef9c3',label:'Moderate', desc:'Getting stuffy — open a window'},
  {max:2000, color:'#f97316',bg:'#ffedd5',label:'Poor',     desc:'Poor ventilation — act soon'},
  {max:1e9,  color:'#ef4444',bg:'#fee2e2',label:'Very Poor',desc:'Very stuffy — ventilate now'},
];
function lvl(v,T){return T.find(l=>v<l.max)||T[T.length-1];}

// ── Chart drawing ─────────────────────────────────────────────────────────────
// data: [{t: epochSec, eco2, tvoc}]
// key: 'tvoc' | 'eco2'
// gapSec: seconds gap threshold to break the line
// range: '5m' | '24h' | '28d'  (controls x-label format)
function drawChart(canvasId, data, key, lineColor, gapSec, range, mmId) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;

  const vals = data.map(d => d[key]);
  const times = data.map(d => d.t);

  // Update min/max label
  if (mmId && vals.length > 0) {
    const lo = Math.min(...vals), hi = Math.max(...vals);
    document.getElementById(mmId).textContent = `min ${lo}  max ${hi}`;
  }

  if (vals.length < 2) return;

  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width  = rect.width  * dpr;
  canvas.height = rect.height * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;

  // Margins: left for Y labels, bottom for X labels
  const ML=46, MR=6, MT=8, MB=20;
  const PW=W-ML-MR, PH=H-MT-MB;

  const lo=Math.min(...vals), hi=Math.max(...vals), span=(hi-lo)||1;
  const px = i => ML + (i/(vals.length-1))*PW;
  // leave 6% headroom at top so the max label isn't clipped
  const py = v => MT + PH*(0.94 - 0.88*(v-lo)/span);

  // Plot area background
  ctx.fillStyle='#f8fafc'; ctx.fillRect(ML,MT,PW,PH);

  // Axis lines
  ctx.strokeStyle='#e2e8f0'; ctx.lineWidth=1;
  ctx.beginPath(); ctx.moveTo(ML,MT); ctx.lineTo(ML,MT+PH); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(ML,MT+PH); ctx.lineTo(ML+PW,MT+PH); ctx.stroke();

  // Y-axis labels: max, mid, min
  ctx.font='10px system-ui,sans-serif';
  ctx.fillStyle='#94a3b8'; ctx.textAlign='right';
  ctx.textBaseline='top';    ctx.fillText(Math.round(hi), ML-4, MT+1);
  ctx.textBaseline='middle'; ctx.fillStyle='#cbd5e1';
                              ctx.fillText(Math.round((lo+hi)/2), ML-4, MT+PH*0.47);
  ctx.textBaseline='bottom'; ctx.fillStyle='#94a3b8';
                              ctx.fillText(Math.round(lo), ML-4, MT+PH-1);

  // X-axis labels (≈5 ticks)
  const xLabels = computeXLabels(times, range);
  ctx.textAlign='center'; ctx.textBaseline='top'; ctx.fillStyle='#94a3b8';
  for(const {idx,label} of xLabels){
    const x=px(idx);
    ctx.strokeStyle='#e2e8f0'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(x,MT+PH); ctx.lineTo(x,MT+PH+4); ctx.stroke();
    ctx.fillText(label, x, MT+PH+5);
  }

  // Data line with gap detection (moveTo lifts the pen)
  ctx.beginPath();
  ctx.strokeStyle=lineColor; ctx.lineWidth=1.8; ctx.lineJoin='round';
  for(let i=0;i<vals.length;i++){
    const x=px(i), y=py(vals[i]);
    const gap = i>0 && (times[i]-times[i-1]) > gapSec;
    if(i===0||gap) ctx.moveTo(x,y); else ctx.lineTo(x,y);
  }
  ctx.stroke();

  // Area fill (re-trace with gap awareness so fill doesn't bridge gaps)
  ctx.globalAlpha=0.12; ctx.fillStyle=lineColor;
  let inPath=false;
  ctx.beginPath();
  for(let i=0;i<vals.length;i++){
    const x=px(i), y=py(vals[i]);
    const gap = i>0 && (times[i]-times[i-1]) > gapSec;
    if(i===0||gap){
      if(inPath){ ctx.lineTo(px(i-1),MT+PH); ctx.closePath(); ctx.fill(); ctx.beginPath(); }
      ctx.moveTo(x,MT+PH); ctx.lineTo(x,y); inPath=true;
    } else { ctx.lineTo(x,y); }
  }
  if(inPath){ ctx.lineTo(px(vals.length-1),MT+PH); ctx.closePath(); ctx.fill(); }
  ctx.globalAlpha=1;

  // Dot at latest point
  const n=vals.length-1;
  ctx.beginPath(); ctx.arc(px(n),py(vals[n]),3.5,0,Math.PI*2);
  ctx.fillStyle=lineColor; ctx.fill();
}

function computeXLabels(times, range){
  if(times.length<2) return [];
  const n=times.length;
  // target ~5 evenly-spaced labels
  const step=Math.max(1,Math.floor(n/5));
  const fmt = range==='24h'
    ? t=>{ const d=new Date(t*1000); return String(d.getHours()).padStart(2,'0')+':00'; }
    : range==='28d'
    ? t=>new Date(t*1000).toLocaleDateString('en-US',{month:'short',day:'numeric'})
    : t=>{ const d=new Date(t*1000);
           return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0'); };
  const labels=[];
  for(let i=0;i<n;i+=step) labels.push({idx:i,label:fmt(times[i])});
  return labels;
}

// ── Alert banner ──────────────────────────────────────────────────────────────
function updateAlert(tvoc, eco2){
  const banner=document.getElementById('alert-banner');
  const tl=lvl(tvoc,TVOC_L), el=lvl(eco2,ECO2_L);
  const msgs=[];
  if(tvoc>=2200) msgs.push(`TVOC is ${tl.label} (${tvoc} ppb) — ${tl.desc}.`);
  if(eco2>=1500) msgs.push(`eCO₂ is ${el.label} (${eco2} ppm) — ${el.desc}.`);
  if(msgs.length){
    banner.textContent=msgs.join(' ');
    banner.className='alert'+(tvoc>=5500||eco2>=2000?' bad':'');
    banner.style.display='block';
  } else {
    banner.style.display='none';
  }
}

// ── Live update (every 2 s) ───────────────────────────────────────────────────
const PHASE={
  starting:['Starting…','ph-starting'],
  warmup:  ['Warming up…','ph-warmup'],
  running: ['Running','ph-running'],
  error:   ['Sensor error','ph-error'],
};

async function fetchLive(){
  try{
    const {current:c,history:h}=await(await fetch('/api/data')).json();
    const[pl,pc]=PHASE[c.phase]||PHASE.starting;
    const b=document.getElementById('badge');
    b.textContent=pl; b.className='badge '+pc;
    if(c.ts) document.getElementById('sub').textContent='SGP30 · Updated '+c.ts.slice(11,19);

    if(c.tvoc!==null){
      const l=lvl(c.tvoc,TVOC_L);
      const v=document.getElementById('tvoc-val');
      v.textContent=c.tvoc; v.style.color=l.color;
      const bg=document.getElementById('tvoc-badge');
      bg.textContent=l.label; bg.style.background=l.bg; bg.style.color=l.color;
      document.getElementById('tvoc-desc').textContent=l.desc;
    }
    if(c.eco2!==null){
      const l=lvl(c.eco2,ECO2_L);
      const v=document.getElementById('eco2-val');
      v.textContent=c.eco2; v.style.color=l.color;
      const bg=document.getElementById('eco2-badge');
      bg.textContent=l.label; bg.style.background=l.bg; bg.style.color=l.color;
      document.getElementById('eco2-desc').textContent=l.desc;
    }
    if(c.tvoc!==null&&c.eco2!==null) updateAlert(c.tvoc,c.eco2);

    if(h.length>1){
      const tc=c.tvoc!==null?lvl(c.tvoc,TVOC_L).color:'#64748b';
      const ec=c.eco2!==null?lvl(c.eco2,ECO2_L).color:'#64748b';
      drawChart('tvoc-live',h,'tvoc',tc,120,'5m','mm-tvoc-live');
      drawChart('eco2-live',h,'eco2',ec,120,'5m','mm-eco2-live');
    }
  }catch(_){
    const b=document.getElementById('badge');
    b.textContent='Connection error'; b.className='badge ph-error';
  }
}

// ── Historical charts (refresh every 5 min) ───────────────────────────────────
async function fetchHistory(range){
  try{
    const {data}=await(await fetch('/api/history?range='+range)).json();
    if(!data||data.length<2) return;
    // For historical charts, color by the last reading's level
    const last=data[data.length-1];
    const tc=lvl(last.tvoc,TVOC_L).color;
    const ec=lvl(last.eco2,ECO2_L).color;
    // gap threshold: 2× log interval (10 min for 24h raw, 3 h for 28d hourly)
    const gapSec=range==='28d'?10800:600;
    drawChart(`tvoc-${range}`,data,'tvoc',tc,gapSec,range,`mm-tvoc-${range}`);
    drawChart(`eco2-${range}`,data,'eco2',ec,gapSec,range,`mm-eco2-${range}`);
  }catch(e){ console.warn('history fetch failed',range,e); }
}

function refreshAll(){
  fetchHistory('24h');
  fetchHistory('28d');
}

fetchLive();
refreshAll();
setInterval(fetchLive, 2000);
setInterval(refreshAll, 300000);  // re-fetch historical every 5 min
window.addEventListener('resize', ()=>{ fetchLive(); refreshAll(); });
</script>
</body>
</html>"""


if __name__ == "__main__":
    threading.Thread(target=sensor_loop, daemon=True).start()
    print(f"Dashboard: http://0.0.0.0:{PORT}", flush=True)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
