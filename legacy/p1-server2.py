#!/usr/bin/env python3
import json
import sqlite3
import threading
import queue
from datetime import datetime, timedelta, timezone
import time
import requests
import logging
import socket
from flask import (
    Flask,
    Response,
    request,
    jsonify,
    render_template_string,
    stream_with_context,
)
from flask_sock import Sock

# =========================
# ---- Konfiguration ------
# =========================
P1_IP = "192.168.10.191" 
POLL_INTERVAL = 10        
DB_PATH = "p1.db"
HOST = "0.0.0.0"
PORT = 8000

# Tysta ner Flasks standard-loggning för varje request
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# =========================
# ---- Databaslager -------
# =========================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS p1_measurements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                measured_at TEXT NOT NULL,
                active_power_w REAL,
                total_import_kwh REAL,
                voltage_l1_v REAL,
                voltage_l2_v REAL,
                voltage_l3_v REAL,
                active_current_l1_a REAL,
                active_current_l2_a REAL,
                active_current_l3_a REAL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_measured_at ON p1_measurements(measured_at)")
        conn.commit()
    finally:
        conn.close()

def insert_measurement(row: dict):
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO p1_measurements (
                measured_at, active_power_w, total_import_kwh,
                voltage_l1_v, voltage_l2_v, voltage_l3_v,
                active_current_l1_a, active_current_l2_a, active_current_l3_a
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row["measured_at"], row.get("active_power_w"), row.get("total_import_kwh"),
            row.get("voltage_l1_v"), row.get("voltage_l2_v"), row.get("voltage_l3_v"),
            row.get("active_current_l1_a"), row.get("active_current_l2_a"), row.get("active_current_l3_a")
        ))
        conn.commit()
    finally:
        conn.close()

def _calculate_extra_fields(p: dict) -> dict:
    i_vals = [p.get("active_current_l1_a") or 0, p.get("active_current_l2_a") or 0, p.get("active_current_l3_a") or 0]
    i_max = max(i_vals)
    p["imbalance_a"] = i_max - min(i_vals)
    p["imbalance_pct"] = (p["imbalance_a"] / i_max * 100.0) if i_max > 0 else 0.0
    p["total_current_a"] = sum(i_vals)
    return p

def query_series(from_dt, aggregate_minute: bool):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        start_iso = from_dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        if aggregate_minute:
            cur.execute("""
                SELECT strftime('%Y-%m-%dT%H:%M:00Z', measured_at) AS ts,
                AVG(active_power_w) as active_power_w, AVG(active_current_l1_a) as active_current_l1_a,
                AVG(active_current_l2_a) as active_current_l2_a, AVG(active_current_l3_a) as active_current_l3_a,
                AVG(voltage_l1_v) as voltage_l1_v, AVG(voltage_l2_v) as voltage_l2_v, AVG(voltage_l3_v) as voltage_l3_v
                FROM p1_measurements WHERE measured_at >= ? GROUP BY ts ORDER BY ts ASC
            """, (start_iso,))
        else:
            cur.execute("SELECT measured_at as ts, * FROM p1_measurements WHERE measured_at >= ? ORDER BY measured_at ASC", (start_iso,))
        return [_calculate_extra_fields(dict(r)) for r in cur.fetchall()]
    finally:
        conn.close()

def query_latest():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT measured_at as ts, * FROM p1_measurements ORDER BY measured_at DESC LIMIT 1").fetchone()
        return _calculate_extra_fields(dict(row)) if row else None
    finally:
        conn.close()

# =========================
# ---- Web & Flask --------
# =========================
app = Flask(__name__)
sock = Sock(app)
subscribers = set()
subs_lock = threading.Lock()

def broadcast(msg):
    with subs_lock:
        for q in list(subscribers):
            try: q.put_nowait(msg)
            except: subscribers.discard(q)

def collector_loop():
    while True:
        try:
            resp = requests.get(f"http://{P1_IP}/api/v1/data", timeout=5).json()
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            row = {
                "ts": now, "measured_at": now,
                "active_power_w": resp.get("active_power_w"),
                "total_import_kwh": resp.get("total_power_import_kwh"),
                "voltage_l1_v": resp.get("active_voltage_l1_v"), "voltage_l2_v": resp.get("active_voltage_l2_v"), "voltage_l3_v": resp.get("active_voltage_l3_v"),
                "active_current_l1_a": resp.get("active_current_l1_a"), "active_current_l2_a": resp.get("active_current_l2_a"), "active_current_l3_a": resp.get("active_current_l3_a"),
            }
            insert_measurement(row)
            broadcast(_calculate_extra_fields(row))
        except Exception as e: print(f"[{datetime.now().strftime('%H:%M:%S')}] Collector error: {e}")
        time.sleep(POLL_INTERVAL)

INDEX_TEMPLATE = r"""<!doctype html>
<html lang="sv">
<head>
  <meta charset="utf-8">
  <title>P1 Monitor</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1"></script>
  <script src="https://cdn.jsdelivr.net/npm/date-fns@3.6.0"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1"></script>
  <style>
    body { font-family: 'Segoe UI', sans-serif; margin: 1.5rem; background: #f8f9fa; }
    .wrap { max-width: 1300px; margin: 0 auto; background: white; padding: 20px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); }
    .controls { display: flex; gap: 10px; margin-bottom: 20px; align-items: center; }
    button, .btn { padding: 8px 15px; border: 1px solid #ccc; background: #fff; cursor: pointer; border-radius: 5px; text-decoration: none; color: #000; font-size: 14px; }
    button.active { background: #007bff; color: #fff; border-color: #0056b3; }
    #btnReset { margin-left: auto; background: #ffeded; color: #c00; }
    #chart-container { height: 500px; width: 100%; }
    .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 15px; margin-top: 20px; }
    .stat-card { background: #f1f3f5; padding: 15px; border-radius: 8px; text-align: center; border: 1px solid #dee2e6; }
    .stat-val { font-size: 1.3rem; font-weight: bold; display: block; margin-top: 5px; color: #2d3748; }
    .stat-label { font-size: 0.75rem; color: #666; text-transform: uppercase; font-weight: bold; }
  </style>
</head>
<body>
  <div class="wrap">
    <h2>Energimonitor P1</h2>
    <div class="controls">
      <button onclick="changeRange(1, this)" class="active">1h</button>
      <button onclick="changeRange(6, this)">6h</button>
      <button onclick="changeRange(24, this)">24h</button>
      <button onclick="changeRange(168, this)">7d</button>
      <a href="/api/export.csv" class="btn">Exportera CSV</a>
      <button id="btnReset" onclick="resetZoom()">Återställ Zoom</button>
    </div>
    <div id="chart-container"><canvas id="chart"></canvas></div>
    <div class="stats">
      <div class="stat-card"><span class="stat-label">Effekt</span><span id="val-w" class="stat-val">- W</span></div>
      <div class="stat-card"><span class="stat-label">Ström (L1/L2/L3)</span><span id="val-a" class="stat-val">- A</span></div>
      <div class="stat-card"><span class="stat-label">Spänning (L1/L2/L3)</span><span id="val-v" class="stat-val">- V</span></div>
      <div class="stat-card"><span class="stat-label">Obalans</span><span id="val-ob" class="stat-val">-</span></div>
    </div>
  </div>

  <script>
    let chart;
    const ctx = document.getElementById('chart').getContext('2d');

    window.addEventListener('load', function() {
      if (window['chartjs-plugin-zoom']) { Chart.register(window['chartjs-plugin-zoom']); }
      initChart(1);
    });

    function updateStats(msg) {
      document.getElementById('val-w').textContent = Math.round(msg.active_power_w) + ' W';
      document.getElementById('val-a').textContent = `${(msg.active_current_l1_a||0).toFixed(1)} / ${(msg.active_current_l2_a||0).toFixed(1)} / ${(msg.active_current_l3_a||0).toFixed(1)} A`;
      document.getElementById('val-v').textContent = `${Math.round(msg.voltage_l1_v||0)} / ${Math.round(msg.voltage_l2_v||0)} / ${Math.round(msg.voltage_l3_v||0)} V`;
      document.getElementById('val-ob').textContent = (msg.imbalance_a || 0).toFixed(1) + ' A (' + (msg.imbalance_pct || 0).toFixed(0) + '%)';
    }

    async function initChart(hours) {
      const res = await fetch(`/api/series?hours=${hours}`);
      const json = await res.json();
      const points = json.points || [];

      const config = {
        type: 'line',
        data: {
          datasets: [
            { label: 'Effekt (W)', data: points.map(p=>({x:new Date(p.ts), y:p.active_power_w})), borderColor: '#2563eb', yAxisID: 'yW', pointRadius: 0, borderWidth: 2 },
            { label: 'L1 (A)', data: points.map(p=>({x:new Date(p.ts), y:p.active_current_l1_a})), borderColor: '#dc2626', yAxisID: 'yA', pointRadius: 0, borderWidth: 1.5 },
            { label: 'L2 (A)', data: points.map(p=>({x:new Date(p.ts), y:p.active_current_l2_a})), borderColor: '#16a34a', yAxisID: 'yA', pointRadius: 0, borderWidth: 1.5 },
            { label: 'L3 (A)', data: points.map(p=>({x:new Date(p.ts), y:p.active_current_l3_a})), borderColor: '#9333ea', yAxisID: 'yA', pointRadius: 0, borderWidth: 1.5 },
            { label: 'L1 (V)', data: points.map(p=>({x:new Date(p.ts), y:p.voltage_l1_v})), borderColor: '#2563eb', yAxisID: 'yV', pointRadius: 0, borderWidth: 1, borderDash: [5,5], hidden: true },
            { label: 'L2 (V)', data: points.map(p=>({x:new Date(p.ts), y:p.voltage_l2_v})), borderColor: '#16a34a', yAxisID: 'yV', pointRadius: 0, borderWidth: 1, borderDash: [5,5], hidden: true },
            { label: 'L3 (V)', data: points.map(p=>({x:new Date(p.ts), y:p.voltage_l3_v})), borderColor: '#9333ea', yAxisID: 'yV', pointRadius: 0, borderWidth: 1, borderDash: [5,5], hidden: true }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          scales: {
            x: { 
                type: 'time', 
                time: { 
                    unit: 'minute',
                    displayFormats: { minute: 'HH:mm', hour: 'HH:mm' }
                },
                ticks: { autoSkip: true, maxRotation: 0 }
            },
            yW: { position: 'left', title: { display: true, text: 'Watt' } },
            yA: { position: 'right', title: { display: true, text: 'Ampere' }, grid: { display: false }, min: 0 },
            yV: { position: 'right', title: { display: true, text: 'Volt' }, grid: { display: false }, min: 200, max: 260, display: 'auto' }
          },
          plugins: {
            zoom: {
              pan: { enabled: true, mode: 'x' },
              zoom: { wheel: { enabled: true, modifierKey: 'ctrl' }, pinch: { enabled: true }, mode: 'x' }
            },
            tooltip: { mode: 'index', intersect: false }
          }
        }
      };

      if (chart) chart.destroy();
      chart = new Chart(ctx, config);
      if (points.length) updateStats(points[points.length-1]);
    }

    function changeRange(h, btn) {
      document.querySelectorAll('.controls button').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      initChart(h);
    }

    function resetZoom() { if(chart) chart.resetZoom(); }

    const ws = new WebSocket((location.protocol==='https:'?'wss':'ws')+'://'+location.host+'/ws');
    ws.onmessage = e => {
      const msg = JSON.parse(e.data);
      updateStats(msg);
      if (chart && chart.data.datasets.length > 0) {
        const t = new Date(msg.ts);
        chart.data.datasets[0].data.push({x: t, y: msg.active_power_w});
        chart.data.datasets[1].data.push({x: t, y: msg.active_current_l1_a});
        chart.data.datasets[2].data.push({x: t, y: msg.active_current_l2_a});
        chart.data.datasets[3].data.push({x: t, y: msg.active_current_l3_a});
        chart.data.datasets[4].data.push({x: t, y: msg.voltage_l1_v});
        chart.data.datasets[5].data.push({x: t, y: msg.voltage_l2_v});
        chart.data.datasets[6].data.push({x: t, y: msg.voltage_l3_v});
        chart.update('none');
      }
    };
  </script>
</body>
</html>
"""

@app.route("/")
def index(): return render_template_string(INDEX_TEMPLATE)

@app.route("/api/series")
def api_series():
    h = request.args.get("hours", 1, type=int)
    return jsonify({"points": query_series(datetime.now(timezone.utc) - timedelta(hours=h), h > 6)})

@app.route("/api/export.csv")
def api_export_csv():
    points = query_series(datetime.now(timezone.utc) - timedelta(hours=24), False)
    def generate():
        yield "Tid;Effekt;L1_A;L2_A;L3_A;L1_V;L2_V;L3_V\n"
        for p in points:
            yield f"{p['ts']};{p['active_power_w']};{p['active_current_l1_a']};{p['active_current_l2_a']};{p['active_current_l3_a']};{p['voltage_l1_v']};{p['voltage_l2_v']};{p['voltage_l3_v']}\n"
    return Response(stream_with_context(generate()), mimetype="text/csv")

@sock.route("/ws")
def ws_route(ws):
    q = queue.Queue(maxsize=100); subscribers.add(q)
    try:
        latest = query_latest()
        if latest: ws.send(json.dumps(latest))
        while True: ws.send(json.dumps(q.get()))
    except: pass
    finally: subscribers.discard(q)

def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try: s.connect(('8.8.8.8', 1)); ip = s.getsockname()[0]
    except: ip = '127.0.0.1'
    finally: s.close()
    return ip

if __name__ == "__main__":
    init_db()
    threading.Thread(target=collector_loop, daemon=True).start()
    
    # Skriv ut webbadressen tydligt i terminalen
    local_ip = get_ip()
    print("\n" + "="*50)
    print(f" P1 MONITOR STARTAD")
    print(f" Lokal adress:  http://localhost:{PORT}")
    print(f" Nätverksadress: http://{local_ip}:{PORT}")
    print("="*50 + "\n")
    
    app.run(host=HOST, port=PORT, threaded=True)