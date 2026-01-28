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

PHASE_LIMIT_A = 16  
VOLT_LOW = 207      
VOLT_HIGH = 253     

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
                "total_import_kwh": resp.get("total_power_import_kwh") or resp.get("total_import_kwh"),
                "voltage_l1_v": resp.get("active_voltage_l1_v") or resp.get("voltage_l1_v"), 
                "voltage_l2_v": resp.get("active_voltage_l2_v") or resp.get("voltage_l2_v"), 
                "voltage_l3_v": resp.get("active_voltage_l3_v") or resp.get("voltage_l3_v"),
                "active_current_l1_a": resp.get("active_current_l1_a"), 
                "active_current_l2_a": resp.get("active_current_l2_a"), 
                "active_current_l3_a": resp.get("active_current_l3_a"),
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
    :root {
      --bg-color: #f8f9fa; --card-bg: #ffffff; --text-color: #333; --border-color: #dee2e6; --stat-card-bg: #f1f3f5;
    }
    [data-theme="dark"] {
      --bg-color: #121212; --card-bg: #1e1e1e; --text-color: #e0e0e0; --border-color: #333; --stat-card-bg: #2d2d2d;
    }
    body { font-family: 'Segoe UI', sans-serif; margin: 1.5rem; background: var(--bg-color); color: var(--text-color); transition: 0.3s; }
    .container { max-width: 1300px; margin: 0 auto; display: flex; flex-direction: column; gap: 20px; }
    .card { background: var(--card-bg); padding: 20px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); border: 1px solid var(--border-color); }
    .header { display: flex; justify-content: space-between; align-items: center; }
    .controls { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    button, .btn { padding: 8px 15px; border: 1px solid var(--border-color); background: var(--card-bg); cursor: pointer; border-radius: 5px; text-decoration: none; color: var(--text-color); font-size: 14px; transition: 0.2s; }
    button:hover, .btn:hover { background: var(--stat-card-bg); }
    button.active { background: #007bff; color: #fff; border-color: #0056b3; }
    .main-grid { display: grid; grid-template-columns: 1fr 350px; gap: 20px; }
    @media (max-width: 1000px) { .main-grid { grid-template-columns: 1fr; } }
    #chart-container { height: 450px; width: 100%; }
    #pie-container { height: 300px; width: 100%; position: relative; }
    .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; }
    .stat-card { background: var(--stat-card-bg); padding: 15px; border-radius: 8px; text-align: center; border: 1px solid var(--border-color); }
    .stat-val { font-size: 1.2rem; font-weight: bold; display: block; margin-top: 5px; }
    .stat-label { font-size: 0.7rem; color: #888; text-transform: uppercase; letter-spacing: 1px; }
    .good { color: #1a7f37; } .warn { color: #a15b00; } .bad { color: #c62828; }
  </style>
</head>
<body>
  <div class="container">
    <div class="card header">
      <h2 style="margin:0">Energimonitor P1</h2>
      <div class="controls">
        <button onclick="toggleTheme()" id="themeBtn">🌙 Mörkt läge</button>
        <button onclick="changeRange(1, this)" class="active">1h</button>
        <button onclick="changeRange(6, this)">6h</button>
        <button onclick="changeRange(24, this)">24h</button>
        <a href="/api/export.csv?hours=24&sep=%3B&decimal=comma" class="btn">Exportera CSV</a>
        <button onclick="downloadPNG()" class="btn">Spara PNG</button>
        <button id="btnReset" onclick="resetZoom()">Återställ Zoom</button>
      </div>
    </div>

    <div class="main-grid">
      <div class="card">
        <div id="chart-container"><canvas id="chart"></canvas></div>
      </div>
      <div class="card" style="display:flex; flex-direction:column; align-items:center; justify-content:center;">
        <h4 style="margin-top:0">Fasfördelning (Just nu)</h4>
        <div id="pie-container"><canvas id="pieChart"></canvas></div>
        <p id="total-a-label" style="font-weight:bold; margin-top:15px; color:#888">Totalt: - A</p>
      </div>
    </div>

    <div class="stats">
      <div class="stat-card"><span class="stat-label">Effekt</span><span id="val-w" class="stat-val">- W</span></div>
      <div class="stat-card"><span class="stat-label">Ström (L1/L2/L3)</span><span id="val-a" class="stat-val">- A</span></div>
      <div class="stat-card"><span class="stat-label">Spänning</span><span id="val-v" class="stat-val">- V</span></div>
      <div class="stat-card"><span class="stat-label">Obalans</span><span id="val-ob" class="stat-val">-</span></div>
    </div>
  </div>

  <script>
    const P1CFG = { limitA: {{ phase_limit_a }}, vLow: {{ volt_low }}, vHigh: {{ volt_high }} };
    let chart, pieChart;
    const ctx = document.getElementById('chart').getContext('2d');
    const pieCtx = document.getElementById('pieChart').getContext('2d');

    function toggleTheme() {
      const target = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      setTheme(target);
    }
    function setTheme(theme) {
      document.documentElement.setAttribute('data-theme', theme);
      localStorage.setItem('p1-theme', theme);
      document.getElementById('themeBtn').textContent = theme === 'dark' ? '☀️ Ljust läge' : '🌙 Mörkt läge';
      if(chart) {
          const color = theme === 'dark' ? '#888' : '#666';
          chart.options.scales.x.ticks.color = color;
          chart.options.scales.yW.ticks.color = color;
          chart.options.scales.yA.ticks.color = color;
          chart.options.plugins.legend.labels.color = theme === 'dark' ? '#aaa' : '#666';
          chart.update('none');
      }
    }

    function downloadPNG() {
      const canvas = document.getElementById('chart');
      const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
      const tempCanvas = document.createElement('canvas');
      tempCanvas.width = canvas.width; tempCanvas.height = canvas.height;
      const tCtx = tempCanvas.getContext('2d');
      tCtx.fillStyle = isDark ? '#1e1e1e' : '#ffffff';
      tCtx.fillRect(0, 0, tempCanvas.width, tempCanvas.height);
      tCtx.drawImage(canvas, 0, 0);
      const link = document.createElement('a');
      link.download = 'p1_graf.png';
      link.href = tempCanvas.toDataURL('image/png');
      link.click();
    }

    function updateStats(msg) {
      document.getElementById('val-w').textContent = Math.round(msg.active_power_w) + ' W';
      document.getElementById('val-a').textContent = `${msg.active_current_l1_a.toFixed(1)} / ${msg.active_current_l2_a.toFixed(1)} / ${msg.active_current_l3_a.toFixed(1)} A`;
      document.getElementById('val-v').textContent = `${Math.round(msg.voltage_l1_v)} / ${Math.round(msg.voltage_l2_v)} / ${Math.round(msg.voltage_l3_v)} V`;
      document.getElementById('total-a-label').textContent = `Totalt: ${msg.total_current_a.toFixed(1)} A`;
      const obPct = msg.imbalance_pct || 0;
      const obEl = document.getElementById('val-ob');
      obEl.textContent = `${msg.imbalance_a.toFixed(1)} A (${obPct.toFixed(0)}%)`;
      obEl.className = 'stat-val ' + (obPct > 20 ? 'bad' : obPct > 10 ? 'warn' : 'good');
      if(pieChart) {
        pieChart.data.datasets[0].data = [msg.active_current_l1_a, msg.active_current_l2_a, msg.active_current_l3_a];
        pieChart.update('none');
      }
    }

    function initPie() {
      pieChart = new Chart(pieCtx, {
        type: 'doughnut',
        data: { labels: ['L1', 'L2', 'L3'], datasets: [{ data: [0, 0, 0], backgroundColor: ['#dc2626', '#16a34a', '#9333ea'], borderWidth: 0 }] },
        options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: 'bottom' } }, cutout: '70%' }
      });
    }

    function getThresholds(points) {
        if(!points.length) return [];
        const start = points[0].x, end = points[points.length-1].x;
        const line = (label, val, axis, color) => ({
            label, data: [{x:start, y:val}, {x:end, y:val}], 
            borderColor: color, borderWidth: 1, borderDash: [5,5], pointRadius: 0, fill: false, yAxisID: axis
        });
        return [line(`Gräns ${P1CFG.limitA}A`, P1CFG.limitA, 'yA', '#ff0000'), line(`V Låg`, P1CFG.vLow, 'yV', '#999'), line(`V Hög`, P1CFG.vHigh, 'yV', '#999')];
    }

    async function initChart(hours) {
      const res = await fetch(`/api/series?hours=${hours}`);
      const json = await res.json();
      const points = json.points || [];
      const chartPoints = points.map(p=>({x:new Date(p.ts), p}));
      const isDark = document.documentElement.getAttribute('data-theme') === 'dark';

      const datasets = [
        { label: 'Effekt (W)', data: chartPoints.map(d=>({x:d.x, y:d.p.active_power_w})), borderColor: '#2563eb', yAxisID: 'yW', pointRadius: 0, borderWidth: 2 },
        { label: 'L1 (A)', data: chartPoints.map(d=>({x:d.x, y:d.p.active_current_l1_a})), borderColor: '#dc2626', yAxisID: 'yA', pointRadius: 0, borderWidth: 1.5 },
        { label: 'L2 (A)', data: chartPoints.map(d=>({x:d.x, y:d.p.active_current_l2_a})), borderColor: '#16a34a', yAxisID: 'yA', pointRadius: 0, borderWidth: 1.5 },
        { label: 'L3 (A)', data: chartPoints.map(d=>({x:d.x, y:d.p.active_current_l3_a})), borderColor: '#9333ea', yAxisID: 'yA', pointRadius: 0, borderWidth: 1.5 },
        { label: 'L1 (V)', data: chartPoints.map(d=>({x:d.x, y:d.p.voltage_l1_v})), borderColor: '#dc2626', yAxisID: 'yV', pointRadius: 0, borderWidth: 1, borderDash: [2,2], hidden: true },
        { label: 'L2 (V)', data: chartPoints.map(d=>({x:d.x, y:d.p.voltage_l2_v})), borderColor: '#16a34a', yAxisID: 'yV', pointRadius: 0, borderWidth: 1, borderDash: [2,2], hidden: true },
        { label: 'L3 (V)', data: chartPoints.map(d=>({x:d.x, y:d.p.voltage_l3_v})), borderColor: '#9333ea', yAxisID: 'yV', pointRadius: 0, borderWidth: 1, borderDash: [2,2], hidden: true }
      ];

      if (chart) chart.destroy();
      chart = new Chart(ctx, {
        type: 'line',
        data: { datasets: [...datasets, ...getThresholds(chartPoints)] },
        options: {
          responsive: true, maintainAspectRatio: false, animation: false,
          scales: {
            x: { 
                type: 'time', 
                time: { unit: 'minute', displayFormats: { minute: 'HH:mm', hour: 'HH:mm' } },
                ticks: { color: isDark ? '#888' : '#666', callback: (v) => new Date(v).toLocaleTimeString('sv-SE', {hour:'2-digit', minute:'2-digit', hour12:false}) }
            },
            yW: { position: 'left', title: { display: true, text: 'Watt' }, ticks: { color: isDark ? '#888' : '#666' } },
            yA: { position: 'right', title: { display: true, text: 'Ampere' }, min: 0, ticks: { color: isDark ? '#888' : '#666' } },
            yV: { position: 'right', title: { display: true, text: 'Volt' }, min: 180, max: 280, display: 'auto' }
          },
          plugins: {
            zoom: { pan: { enabled: true, mode: 'x' }, zoom: { wheel: { enabled: true }, drag: { enabled: true }, mode: 'x' } },
            legend: { labels: { color: isDark ? '#aaa' : '#666' } },
            tooltip: { mode: 'index', intersect: false }
          }
        }
      });
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
      if (chart) {
        const t = new Date(msg.ts);
        chart.data.datasets[0].data.push({x: t, y: msg.active_power_w});
        chart.data.datasets[1].data.push({x: t, y: msg.active_current_l1_a});
        chart.data.datasets[2].data.push({x: t, y: msg.active_current_l2_a});
        chart.data.datasets[3].data.push({x: t, y: msg.active_current_l3_a});
        for(let j=7; j<10; j++) if(chart.data.datasets[j]) chart.data.datasets[j].data[1].x = t;
        chart.update('none');
      }
    };
    window.addEventListener('load', () => { 
        if (window['chartjs-plugin-zoom']) Chart.register(window['chartjs-plugin-zoom']);
        setTheme(localStorage.getItem('p1-theme') || 'light');
        initPie();
        initChart(1); 
    });
  </script>
</body>
</html>
"""

@app.route("/")
def index(): 
    return render_template_string(INDEX_TEMPLATE, phase_limit_a=PHASE_LIMIT_A, volt_low=VOLT_LOW, volt_high=VOLT_HIGH)

@app.route("/api/series")
def api_series():
    h = request.args.get("hours", 1, type=int)
    return jsonify({"points": query_series(datetime.now(timezone.utc) - timedelta(hours=h), h > 6)})

@app.route("/api/export.csv")
def api_export_csv():
    hours = request.args.get("hours", 24, type=int)
    sep = request.args.get("sep", ";")
    decimal_comma = request.args.get("decimal") == "comma"
    points = query_series(datetime.now(timezone.utc) - timedelta(hours=hours), False)
    def generate():
        yield sep.join(["Tid", "Effekt_W", "L1_A", "L2_A", "L3_A", "L1_V", "L2_V", "L3_V", "Obalans_A", "Obalans_Pct"]) + "\n"
        for p in points:
            vals = [p['ts'], p['active_power_w'], p['active_current_l1_a'], p['active_current_l2_a'], p['active_current_l3_a'], p['voltage_l1_v'], p['voltage_l2_v'], p['voltage_l3_v'], p['imbalance_a'], p['imbalance_pct']]
            row = [str(v).replace('.', ',') if decimal_comma and isinstance(v, float) else str(v) for v in vals]
            yield sep.join(row) + "\n"
    return Response(stream_with_context(generate()), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=p1_export_{hours}h.csv"})

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
    local_ip = get_ip()
    print(f"\n{'='*50}\n P1 MONITOR STARTAD\n Adress: http://{local_ip}:{PORT}\n{'='*50}\n")
    app.run(host=HOST, port=PORT, threaded=True)