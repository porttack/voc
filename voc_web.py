#!/usr/bin/env python3
"""
SGP30 VOC web dashboard — live air quality monitor on port 8080.

The sensor thread runs at 1 Hz in the background (required by the SGP30
algorithm). The Flask server reads from a shared ring buffer; no sensor
access happens in request handlers.
"""

import collections
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify

from sgp30 import SGP30

BASELINE_FILE = "baseline.json"
BASELINE_MAX_AGE_DAYS = 7
WARMUP_SECONDS = 15
MAX_HISTORY = 300   # 5 minutes at 1 Hz
PORT = 8080

app = Flask(__name__)
_lock = threading.Lock()
_history: collections.deque = collections.deque(maxlen=MAX_HISTORY)
_state: dict = {"eco2": None, "tvoc": None, "ts": None, "phase": "starting"}


def _load_baseline(sensor: SGP30) -> None:
    if not os.path.exists(BASELINE_FILE):
        return
    try:
        with open(BASELINE_FILE) as f:
            data = json.load(f)
        saved_at = datetime.fromisoformat(data["saved_at"])
        age_days = (datetime.now(timezone.utc) - saved_at).total_seconds() / 86400
        if age_days <= BASELINE_MAX_AGE_DAYS:
            sensor.set_baseline(data["eco2_baseline"], data["tvoc_baseline"])
            print(f"Loaded baseline (age {age_days:.1f} d)", flush=True)
    except Exception as exc:
        print(f"Could not load baseline: {exc}", file=sys.stderr, flush=True)


def _save_baseline(sensor: SGP30) -> None:
    try:
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
        print(f"Baseline saved: eCO2={eco2_base}, TVOC={tvoc_base}", flush=True)
    except Exception as exc:
        print(f"Could not save baseline: {exc}", file=sys.stderr, flush=True)


def sensor_loop() -> None:
    """Runs forever; reconnects automatically on I2C errors."""
    while True:
        try:
            with SGP30() as sensor:
                sensor.iaq_init()
                _load_baseline(sensor)

                with _lock:
                    _state["phase"] = "warmup"
                print(f"Warming up for {WARMUP_SECONDS} s…", flush=True)
                for _ in range(WARMUP_SECONDS):
                    sensor.measure_iaq()
                    time.sleep(1)

                with _lock:
                    _state["phase"] = "running"
                print("Running.", flush=True)

                last_save = time.monotonic()
                while True:
                    eco2, tvoc = sensor.measure_iaq()
                    ts = datetime.now().isoformat(timespec="seconds")
                    with _lock:
                        _state.update({"eco2": eco2, "tvoc": tvoc, "ts": ts})
                        _history.append({"eco2": eco2, "tvoc": tvoc, "ts": ts})
                    if time.monotonic() - last_save >= 3600:
                        _save_baseline(sensor)
                        last_save = time.monotonic()
                    time.sleep(1)

        except Exception as exc:
            print(f"Sensor error: {exc} — retrying in 5 s", file=sys.stderr, flush=True)
            with _lock:
                _state["phase"] = "error"
            time.sleep(5)


@app.route("/api/data")
def api_data():
    with _lock:
        return jsonify({"current": dict(_state), "history": list(_history)})


@app.route("/")
def index():
    return _HTML


# ---------------------------------------------------------------------------
# Embedded HTML / CSS / JS  (no CDN dependencies)
# ---------------------------------------------------------------------------
_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Air Quality Monitor</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:#f1f5f9;color:#1e293b;padding:16px;min-height:100vh}
.wrap{max-width:760px;margin:0 auto}
header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px}
h1{font-size:1.35rem;font-weight:700;color:#0f172a}
.sub{font-size:.78rem;color:#64748b;margin-bottom:20px}
.badge{font-size:.75rem;font-weight:600;padding:4px 11px;border-radius:999px;white-space:nowrap}
.ph-starting{background:#e2e8f0;color:#64748b}
.ph-warmup  {background:#fef9c3;color:#854d0e}
.ph-running {background:#dcfce7;color:#166534}
.ph-error   {background:#fee2e2;color:#991b1b}
.cards{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
@media(max-width:460px){.cards{grid-template-columns:1fr}}
.card{background:#fff;border-radius:12px;padding:20px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.clabel{font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;
  color:#64748b;margin-bottom:8px}
.cval{font-size:2.8rem;font-weight:700;line-height:1;color:#0f172a;margin-bottom:3px;
  transition:color .4s}
.cunit{font-size:.8rem;color:#94a3b8;margin-bottom:12px}
.cbadge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:.75rem;
  font-weight:600;margin-bottom:5px}
.cdesc{font-size:.75rem;color:#64748b;min-height:1em}
.panel{background:#fff;border-radius:12px;padding:20px;
  box-shadow:0 1px 3px rgba(0,0,0,.08);margin-bottom:16px}
.panel-title{font-size:.85rem;font-weight:600;color:#374151;margin-bottom:14px}
.crow{margin-bottom:14px}
.clabel2{font-size:.72rem;color:#94a3b8;margin-bottom:3px}
canvas{width:100%;height:56px;display:block;border-radius:6px;background:#f8fafc}
.info h2{font-size:1rem;font-weight:700;color:#0f172a;margin-bottom:14px}
.info h3{font-size:.88rem;font-weight:600;color:#334155;margin:18px 0 7px}
.info p{font-size:.82rem;color:#475569;line-height:1.65;margin-bottom:10px}
table{width:100%;border-collapse:collapse;font-size:.78rem;margin-bottom:6px}
th{text-align:left;padding:6px 8px;background:#f8fafc;color:#64748b;
  font-weight:600;border-bottom:1px solid #e2e8f0}
td{padding:6px 8px;border-bottom:1px solid #f1f5f9;color:#374151}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;
  vertical-align:middle}
.note{background:#f0f9ff;border-left:3px solid #38bdf8;padding:10px 14px;
  border-radius:0 6px 6px 0;font-size:.78rem;color:#0c4a6e;margin-top:14px;line-height:1.6}
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div>
      <h1>Air Quality Monitor</h1>
    </div>
    <span class="badge ph-starting" id="badge">Starting</span>
  </header>
  <div class="sub" id="sub">Connecting to sensor&hellip;</div>

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

  <div class="panel">
    <div class="panel-title">Last 5 Minutes</div>
    <div class="crow">
      <div class="clabel2">TVOC (ppb)</div>
      <canvas id="tvoc-chart"></canvas>
    </div>
    <div class="crow" style="margin-bottom:0">
      <div class="clabel2">eCO&sup2; (ppm)</div>
      <canvas id="eco2-chart"></canvas>
    </div>
  </div>

  <div class="panel info">
    <h2>What do these numbers mean?</h2>

    <h3>TVOC &mdash; Total Volatile Organic Compounds</h3>
    <p>VOCs are gases released by everyday items: cleaning products, paints,
    adhesives, furniture, cooking, and even people breathing. Prolonged
    exposure to high levels can cause headaches, dizziness, and irritation.
    Clean outdoor air typically reads 50&ndash;150&nbsp;ppb.</p>
    <table>
      <tr><th>Rating</th><th>TVOC</th><th>Meaning</th></tr>
      <tr><td><span class="dot" style="background:#10b981"></span>Excellent</td><td>0 &ndash; 220 ppb</td><td>Clean air, typical outdoors</td></tr>
      <tr><td><span class="dot" style="background:#22c55e"></span>Good</td><td>220 &ndash; 660 ppb</td><td>Acceptable indoor air</td></tr>
      <tr><td><span class="dot" style="background:#f59e0b"></span>Moderate</td><td>660 &ndash; 2200 ppb</td><td>Some pollutants &mdash; open a window</td></tr>
      <tr><td><span class="dot" style="background:#f97316"></span>Poor</td><td>2200 &ndash; 5500 ppb</td><td>High pollution &mdash; ventilate now</td></tr>
      <tr><td><span class="dot" style="background:#ef4444"></span>Very Poor</td><td>&gt; 5500 ppb</td><td>Harmful &mdash; ventilate immediately</td></tr>
    </table>

    <h3>eCO&sup2; &mdash; Equivalent CO&sup2;</h3>
    <p>The SGP30 <em>estimates</em> CO&sup2; from its VOC sensor &mdash; it is not
    a dedicated CO&sup2; sensor. In normal indoor conditions the estimate tracks
    real CO&sup2; well and is a practical proxy for ventilation quality.
    Outdoor air is roughly 400&ndash;420&nbsp;ppm; an occupied room with poor
    ventilation climbs past 1000&nbsp;ppm within an hour.</p>
    <table>
      <tr><th>Rating</th><th>eCO&sup2;</th><th>Meaning</th></tr>
      <tr><td><span class="dot" style="background:#10b981"></span>Excellent</td><td>400 &ndash; 600 ppm</td><td>Well-ventilated, fresh air</td></tr>
      <tr><td><span class="dot" style="background:#22c55e"></span>Good</td><td>600 &ndash; 1000 ppm</td><td>Acceptable ventilation</td></tr>
      <tr><td><span class="dot" style="background:#f59e0b"></span>Moderate</td><td>1000 &ndash; 1500 ppm</td><td>Getting stuffy &mdash; ventilate</td></tr>
      <tr><td><span class="dot" style="background:#f97316"></span>Poor</td><td>1500 &ndash; 2000 ppm</td><td>Poor ventilation &mdash; act soon</td></tr>
      <tr><td><span class="dot" style="background:#ef4444"></span>Very Poor</td><td>&gt; 2000 ppm</td><td>Very stuffy &mdash; ventilate now</td></tr>
    </table>

    <div class="note">
      <strong>Calibration note:</strong> The SGP30 takes 15 seconds to warm up
      after power-on and up to 12 hours to fully establish its baseline. Readings
      during the first hour may read slightly elevated. For best accuracy leave
      the monitor running continuously; it saves a baseline automatically every
      hour which is restored on the next startup.
    </div>
  </div>

</div><!-- /wrap -->
<script>
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

function lvl(v,table){return table.find(l=>v<l.max)||table[table.length-1];}

function sparkline(id,vals,color){
  const c=document.getElementById(id);
  if(!c||vals.length<2)return;
  const dpr=window.devicePixelRatio||1;
  const r=c.getBoundingClientRect();
  c.width=r.width*dpr; c.height=r.height*dpr;
  const x=c.getContext('2d');
  x.scale(dpr,dpr);
  const W=r.width,H=r.height,P=4;
  const mn=Math.min(...vals),mx=Math.max(...vals),rng=(mx-mn)||1;
  const tx=i=>(i/(vals.length-1))*(W-P*2)+P;
  const ty=v=>H-P-((v-mn)/rng)*(H-P*2);
  x.beginPath();
  x.moveTo(tx(0),ty(vals[0]));
  for(let i=1;i<vals.length;i++)x.lineTo(tx(i),ty(vals[i]));
  x.lineTo(tx(vals.length-1),H);x.lineTo(tx(0),H);x.closePath();
  x.fillStyle=color+'22';x.fill();
  x.beginPath();
  x.moveTo(tx(0),ty(vals[0]));
  for(let i=1;i<vals.length;i++)x.lineTo(tx(i),ty(vals[i]));
  x.strokeStyle=color;x.lineWidth=2;x.lineJoin='round';x.stroke();
  const n=vals.length-1;
  x.beginPath();x.arc(tx(n),ty(vals[n]),3.5,0,Math.PI*2);
  x.fillStyle=color;x.fill();
}

const PHASE={
  starting:['Starting…','ph-starting'],
  warmup:  ['Warming up…','ph-warmup'],
  running: ['Running','ph-running'],
  error:   ['Sensor error','ph-error'],
};

async function poll(){
  try{
    const d=await(await fetch('/api/data')).json();
    const{current:c,history:h}=d;
    const[pl,pc]=PHASE[c.phase]||PHASE.starting;
    const b=document.getElementById('badge');
    b.textContent=pl;b.className='badge '+pc;
    if(c.ts)document.getElementById('sub').textContent=
      'SGP30 · Updated '+c.ts.slice(11,19);
    if(c.tvoc!==null){
      const l=lvl(c.tvoc,TVOC_L);
      const v=document.getElementById('tvoc-val');
      v.textContent=c.tvoc;v.style.color=l.color;
      const bg=document.getElementById('tvoc-badge');
      bg.textContent=l.label;bg.style.background=l.bg;bg.style.color=l.color;
      document.getElementById('tvoc-desc').textContent=l.desc;
    }
    if(c.eco2!==null){
      const l=lvl(c.eco2,ECO2_L);
      const v=document.getElementById('eco2-val');
      v.textContent=c.eco2;v.style.color=l.color;
      const bg=document.getElementById('eco2-badge');
      bg.textContent=l.label;bg.style.background=l.bg;bg.style.color=l.color;
      document.getElementById('eco2-desc').textContent=l.desc;
    }
    if(h.length>1){
      const tc=c.tvoc!==null?lvl(c.tvoc,TVOC_L).color:'#64748b';
      const ec=c.eco2!==null?lvl(c.eco2,ECO2_L).color:'#64748b';
      sparkline('tvoc-chart',h.map(r=>r.tvoc),tc);
      sparkline('eco2-chart',h.map(r=>r.eco2),ec);
    }
  }catch(_){
    const b=document.getElementById('badge');
    b.textContent='Connection error';b.className='badge ph-error';
  }
}
poll();setInterval(poll,2000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    threading.Thread(target=sensor_loop, daemon=True).start()
    print(f"Dashboard: http://0.0.0.0:{PORT}", flush=True)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
