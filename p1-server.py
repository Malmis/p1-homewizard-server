#!/usr/bin/env python3
import json, sqlite3, threading, queue, time, requests, logging, socket, os
from datetime import datetime, timedelta, timezone
from flask import Flask, Response, request, jsonify, render_template_string, cli
from flask_sock import Sock

# --- Tysta ner terminalen ---
cli.show_server_banner = lambda *args: None 
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# --- Konfiguration ---
P1_IP = "192.168.10.191"
DB_PATH = "p1.db"
ELOMRADE = "SE3"
PORT = 8000
current_prices = {}

# --- Databas ---
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS p1_measurements (
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            measured_at TEXT, 
            active_power_w REAL, 
            total_import_kwh REAL, 
            voltage_l1_v REAL, voltage_l2_v REAL, voltage_l3_v REAL, 
            active_current_l1_a REAL, active_current_l2_a REAL, active_current_l3_a REAL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS daily_prices (
            date_str TEXT PRIMARY KEY,
            json_data TEXT)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_measured_at ON p1_measurements(measured_at)")

# --- Elpris-motor ---
def get_prices_for_date(date_obj):
    ds = date_obj.strftime('%Y-%m-%d')
    with sqlite3.connect(DB_PATH) as conn:
        res = conn.execute("SELECT json_data FROM daily_prices WHERE date_str = ?", (ds,)).fetchone()
        if res: return json.loads(res[0])
    try:
        url = f"https://www.elprisetjustnu.se/api/v1/prices/{date_obj.year}/{date_obj.strftime('%m-%d')}_{ELOMRADE}.json"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            prices = {datetime.fromisoformat(p['time_start']).strftime('%H'): float(p.get('SEK_per_kWh') or p.get('sek_per_kwh')) for p in r.json()}
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("INSERT OR REPLACE INTO daily_prices (date_str, json_data) VALUES (?, ?)", (ds, json.dumps(prices)))
            return prices
    except: pass
    return {}

def elpris_scheduler():
    global current_prices
    while True:
        current_prices = get_prices_for_date(datetime.now())
        time.sleep(3600)

def calculate_period_stats(start_dt, end_dt):
    total_cost, total_kwh = 0.0, 0.0
    curr = start_dt.replace(hour=0, minute=0, second=0)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        while curr <= end_dt:
            prices = get_prices_for_date(curr)
            ds, de = curr.strftime('%Y-%m-%dT00:00:00Z'), curr.strftime('%Y-%m-%dT23:59:59Z')
            rows = conn.execute("SELECT measured_at, total_import_kwh FROM p1_measurements WHERE measured_at BETWEEN ? AND ? ORDER BY measured_at ASC", (ds, de)).fetchall()
            for i in range(1, len(rows)):
                v1, v2 = rows[i]['total_import_kwh'], rows[i-1]['total_import_kwh']
                if v1 is not None and v2 is not None:
                    diff = v1 - v2
                    if 0 < diff < 50:
                        h = datetime.fromisoformat(rows[i]['measured_at'].replace("Z", "+00:00")).astimezone().strftime('%H')
                        total_cost += diff * prices.get(h, 0)
                        total_kwh += diff
            curr += timedelta(days=1)
    return round(total_cost, 2), round(total_kwh, 2)

app = Flask(__name__)
sock = Sock(app)
subscribers = set()

def collector_loop():
    while True:
        try:
            r = requests.get(f"http://{P1_IP}/api/v1/data", timeout=5).json()
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            v1, v2, v3 = r.get("active_voltage_l1_v", 0), r.get("active_voltage_l2_v", 0), r.get("active_voltage_l3_v", 0)
            vals = [now, r.get("active_power_w", 0), r.get("total_power_import_kwh") or r.get("total_import_kwh"), 
                    v1, v2, v3, r.get("active_current_l1_a", 0), r.get("active_current_l2_a", 0), r.get("active_current_l3_a", 0)]
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("INSERT INTO p1_measurements (measured_at, active_power_w, total_import_kwh, voltage_l1_v, voltage_l2_v, voltage_l3_v, active_current_l1_a, active_current_l2_a, active_current_l3_a) VALUES (?,?,?,?,?,?,?,?,?)", vals)
            p = {"measured_at": now, "active_power_w": vals[1], "voltage_l1_v": v1, "voltage_l2_v": v2, "voltage_l3_v": v3, 
                 "active_current_l1_a": vals[6], "active_current_l2_a": vals[7], "active_current_l3_a": vals[8],
                 "total_current_a": sum(vals[6:9]), "price_sek_kwh": current_prices.get(datetime.now().strftime('%H'), 0)}
            for q in list(subscribers): q.put_nowait(p)
        except: pass
        time.sleep(10)

@app.route("/")
def index(): return render_template_string(INDEX_HTML)

@app.route("/api/history")
def api_history():
    d_str = request.args.get("date")
    ld = datetime.strptime(d_str, '%Y-%m-%d')
    prices = get_prices_for_date(ld)
    day_cost, day_kwh, hourly_kwh = 0.0, 0.0, {f"{h:02}": 0.0 for h in range(24)}
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        pts = conn.execute("SELECT * FROM p1_measurements WHERE measured_at BETWEEN ? AND ? ORDER BY measured_at ASC", (ld.strftime('%Y-%m-%dT00:00:00Z'), ld.strftime('%Y-%m-%dT23:59:59Z'))).fetchall()
        for i in range(1, len(pts)):
            v1, v2 = pts[i]['total_import_kwh'], pts[i-1]['total_import_kwh']
            if v1 is not None and v2 is not None:
                diff = v1 - v2
                if 0 < diff < 50:
                    h = datetime.fromisoformat(pts[i]['measured_at'].replace("Z", "+00:00")).astimezone().strftime('%H')
                    day_cost += diff * prices.get(h, 0)
                    day_kwh += diff
                    hourly_kwh[h] += diff
    mon_cost, mon_kwh = calculate_period_stats(ld.replace(day=1), ld)
    return jsonify({"total_kwh": round(day_kwh, 2), "total_cost": round(day_cost, 2), "monthly_kwh": mon_kwh, "monthly_cost": mon_cost, "prices": prices, "hourly_kwh": hourly_kwh, "points": [dict(p) for p in pts]})

@app.route("/api/series")
def api_series():
    h = request.args.get("hours", 1, type=int)
    s = (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat().replace("+00:00", "Z")
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return jsonify({"points": [dict(r) for r in conn.execute("SELECT * FROM p1_measurements WHERE measured_at >= ? ORDER BY measured_at ASC", (s,)).fetchall()]})

@sock.route("/ws")
def ws_route(ws):
    q = queue.Queue(maxsize=100); subscribers.add(q)
    try:
        while True: ws.send(json.dumps(q.get()))
    except: pass
    finally: subscribers.discard(q)

INDEX_HTML = r"""<!doctype html>
<html lang="sv">
<head>
  <meta charset="utf-8"><title>P1 Monitor Pro</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1"></script>
  <script src="https://cdn.jsdelivr.net/npm/date-fns@3.6.0"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1"></script>
  <style>
    :root { --bg: #f4f4f9; --card: #fff; --text: #333; --border: #ddd; --stat: #eee; }
    body.dark { --bg: #121212; --card: #1e1e1e; --text: #e0e0e0; --border: #333; --stat: #2d2d2d; }
    body { font-family: sans-serif; margin: 20px; background: var(--bg); color: var(--text); }
    .container { max-width: 1400px; margin: 0 auto; display: flex; flex-direction: column; gap: 20px; }
    .card { background: var(--card); padding: 20px; border-radius: 8px; border: 1px solid var(--border); }
    .main-grid { display: grid; grid-template-columns: 1fr 350px; gap: 20px; }
    .controls { display: flex; gap: 10px; align-items: center; }
    .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-top: 10px; }
    .stat-card { background: var(--stat); padding: 15px; border-radius: 5px; text-align: center; }
    .stat-val { font-size: 1.2em; font-weight: bold; display: block; }
    .chart-container { height: 450px; position: relative; }
    button, .btn { padding: 8px 12px; cursor: pointer; border: 1px solid var(--border); background: var(--card); color: var(--text); border-radius: 5px; }
    button.active { background: #007bff; color: #fff; }
    .footer-kred { margin-top: 20px; text-align: center; opacity: 0.8; font-size: 0.9em; }
    .footer-kred a { color: inherit; text-decoration: none; }
  </style>
</head>
<body>
  <div class="container">
    <div class="card" style="display:flex; justify-content:space-between; align-items:center;">
      <h2 style="margin:0">Energimonitor P1</h2>
      <div class="controls">
        <button onclick="toggleTheme()">🌓 Tema</button>
        <button onclick="changeRange(1, this)" class="active">1h</button>
        <button onclick="changeRange(24, this)">24h</button>
        <button onclick="savePNG('chart')" class="btn">Spara PNG</button>
        <button onclick="chart.resetZoom()" class="btn">Reset Zoom</button>
      </div>
    </div>
    <div class="main-grid">
      <div class="card"><div class="chart-container"><canvas id="chart"></canvas></div></div>
      <div class="card" style="text-align:center; display: flex; flex-direction: column; justify-content: space-between;">
        <h4>Fasbalans</h4>
        <div style="height:200px;"><canvas id="pie"></canvas></div>
        <div style="margin-top: 15px; padding: 10px; background: var(--stat); border-radius: 5px; display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 5px;">
            <div><small style="color:#dc2626">● L1</small><br><span id="phase-l1" style="font-weight:bold">-</span></div>
            <div><small style="color:#16a34a">● L2</small><br><span id="phase-l2" style="font-weight:bold">-</span></div>
            <div><small style="color:#9333ea">● L3</small><br><span id="phase-l3" style="font-weight:bold">-</span></div>
        </div>
        <div style="margin-top: 10px; font-size: 0.9em;">
            <p id="total-a" style="font-weight:bold; margin: 5px 0;">Totalt: - A</p>
            <p style="margin: 0; border-top: 1px solid var(--border); padding-top: 5px;">
                Max obalans: <span id="phase-imbalance" style="font-weight:bold; color: #f59e0b;">- A</span>
            </p>
        </div>
      </div>
    </div>
    <div class="stats">
      <div class="stat-card">Effekt<span id="val-w" class="stat-val">- W</span></div>
      <div class="stat-card">Ström (L1 / L2 / L3)<span id="val-a" class="stat-val">- A</span></div>
      <div class="stat-card">Spänning (L1 / L2 / L3)<span id="val-v-multi" class="stat-val">- V</span></div>
      <div class="stat-card">Pris nu<span id="val-price" class="stat-val">- kr</span></div>
    </div>
    <div class="card">
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <h3>Historik & Ekonomi</h3>
        <div class="controls">
            <input type="date" id="hDate" onchange="loadHistory()">
            <button onclick="exportCSV()" class="btn">Exportera CSV</button>
            <button onclick="savePNG('pChart')" class="btn">Spara PNG</button>
        </div>
      </div>
      <div class="stats">
          <div class="stat-card" style="border-bottom: 4px solid #10b981;">Dygn (kWh)<span id="hKwh" class="stat-val">-</span></div>
          <div class="stat-card" style="border-bottom: 4px solid #3b82f6;">Dygn (kr)<span id="hCost" class="stat-val">-</span></div>
          <div class="stat-card" style="border-bottom: 4px solid #8b5cf6;">Månad (kWh)<span id="monKwh" class="stat-val">-</span></div>
          <div class="stat-card" style="border-bottom: 4px solid #d946ef;">Månad (kr)<span id="monCost" class="stat-val">-</span></div>
      </div>
      <div style="display:grid; grid-template-columns: 1fr 1fr; gap:20px; margin-top:20px;">
        <div class="chart-container"><canvas id="hChart"></canvas></div>
        <div class="chart-container"><canvas id="pChart"></canvas></div>
      </div>
      <div class="footer-kred">
        <p><a href="https://www.elprisetjustnu.se" target="_blank"><img src="https://i.bnfcl.io/hva-koster-strommen/elpriser-tillhandahalls-av-elprisetjustnu_ttNExOIU_.png" alt="Elpriser" width="200" height="45"></a></p>
        <p>Hårdvara: <a href="https://www.homewizard.com/" target="_blank">HomeWizard P1 Wi-Fi Meter</a></p>
      </div>
    </div>
  </div>
  <script>
    let chart, pie, hChart, pChart;
    let lastHistoryData = null;
    let visibleSeries = {};
    const timeOptions = { type: 'time', time: { displayFormats: { hour: 'HH:mm', minute: 'HH:mm' }, unit: 'minute' }, ticks: { source: 'auto' } };

    function toggleTheme() {
      const isDark = document.body.classList.toggle('dark');
      localStorage.setItem('theme', isDark ? 'dark' : 'light');
    }

    function applySavedTheme() {
      if (localStorage.getItem('theme') === 'dark') document.body.classList.add('dark');
    }

    function savePNG(id) {
      const l = document.createElement('a');
      l.download = id+'_'+new Date().toISOString().slice(0,19)+'.png';
      l.href = document.getElementById(id).toDataURL();
      l.click();
    }

    function exportCSV() {
      if (!lastHistoryData || !lastHistoryData.points.length) return alert('Ingen data');
      let csv = '\ufeffTid;Effekt_W;Total_kWh;L1_V;L2_V;L3_V;L1_A;L2_A;L3_A\n';
      lastHistoryData.points.forEach(p => {
        csv += `${p.measured_at};${p.active_power_w};${p.total_import_kwh};${p.voltage_l1_v};${p.voltage_l2_v};${p.voltage_l3_v};${p.active_current_l1_a};${p.active_current_l2_a};${p.active_current_l3_a}\n`;
      });
      const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = `p1_export_${document.getElementById('hDate').value}.csv`;
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
    }

    async function initChart(hours=1) {
      const res = await fetch('/api/series?hours=' + hours);
      const data = await res.json();
      if(chart) {
        chart.data.datasets.forEach((ds, i) => visibleSeries[ds.label] = chart.isDatasetVisible(i));
        chart.destroy();
      }
      chart = new Chart(document.getElementById('chart'), {
        type: 'line',
        data: { datasets: [
            { label: 'Watt', data: data.points.map(p=>({x:new Date(p.measured_at), y:p.active_power_w})), borderColor: '#2563eb', yAxisID: 'yW', pointRadius: 0, fill: true, backgroundColor: 'rgba(37,99,235,0.1)' },
            { label: 'L1 (A)', data: data.points.map(p=>({x:new Date(p.measured_at), y:p.active_current_l1_a})), borderColor: '#dc2626', yAxisID: 'yA', pointRadius: 0 },
            { label: 'L2 (A)', data: data.points.map(p=>({x:new Date(p.measured_at), y:p.active_current_l2_a})), borderColor: '#16a34a', yAxisID: 'yA', pointRadius: 0 },
            { label: 'L3 (A)', data: data.points.map(p=>({x:new Date(p.measured_at), y:p.active_current_l3_a})), borderColor: '#9333ea', yAxisID: 'yA', pointRadius: 0 },
            { label: 'L1 (V)', data: data.points.map(p=>({x:new Date(p.measured_at), y:p.voltage_l1_v})), borderColor: '#f87171', yAxisID: 'yV', pointRadius: 0, borderDash: [2,2], hidden: true },
            { label: 'L2 (V)', data: data.points.map(p=>({x:new Date(p.measured_at), y:p.voltage_l2_v})), borderColor: '#4ade80', yAxisID: 'yV', pointRadius: 0, borderDash: [2,2], hidden: true },
            { label: 'L3 (V)', data: data.points.map(p=>({x:new Date(p.measured_at), y:p.voltage_l3_v})), borderColor: '#c084fc', yAxisID: 'yV', pointRadius: 0, borderDash: [2,2], hidden: true }
        ]},
        options: { 
            responsive: true, maintainAspectRatio: false, 
            scales: { 
                x: { ...timeOptions, time: { ...timeOptions.time, unit: hours > 6 ? 'hour' : 'minute' } }, 
                yW: { position: 'left' }, 
                yA: { position: 'right', min: 0 },
                yV: { position: 'right', display: false, min: 200, max: 260 } 
            },
            plugins: { zoom: { pan: { enabled: true }, zoom: { wheel: { enabled: true }, mode: 'x' } } }
        }
      });
      chart.data.datasets.forEach((ds, i) => { if(visibleSeries[ds.label] !== undefined) chart.setDatasetVisibility(i, visibleSeries[ds.label]); });
      chart.update();
    }

    function changeRange(h, b) { document.querySelectorAll('.controls button').forEach(x=>x.classList.remove('active')); b.classList.add('active'); initChart(h); }

    async function loadHistory() {
      const d = document.getElementById('hDate').value;
      const res = await fetch('/api/history?date=' + d);
      const data = await res.json();
      lastHistoryData = data;
      document.getElementById('hCost').innerText = data.total_cost.toFixed(2) + ' kr';
      document.getElementById('hKwh').innerText = data.total_kwh.toFixed(2) + ' kWh';
      document.getElementById('monKwh').innerText = data.monthly_kwh.toFixed(1) + ' kWh';
      document.getElementById('monCost').innerText = data.monthly_cost.toFixed(2) + ' kr';
      if(hChart) hChart.destroy();
      hChart = new Chart(document.getElementById('hChart'), {
        type: 'line',
        data: { datasets: [{ label: 'Watt Historik', data: data.points.map(p=>({x:new Date(p.measured_at), y:p.active_power_w})), borderColor: '#2563eb', pointRadius: 0 }]},
        options: { responsive: true, maintainAspectRatio: false, scales: { x: { ...timeOptions, time: { ...timeOptions.time, unit: 'hour' } } } }
      });
      if(pChart) pChart.destroy();
      const hours = Object.keys(data.prices).sort();
      let roll = 0;
      const rollData = hours.map(h => { roll += (data.hourly_kwh[h] || 0) * data.prices[h]; return roll; });
      pChart = new Chart(document.getElementById('pChart'), {
        data: {
          labels: hours.map(h => h+':00'),
          datasets: [
            { type: 'bar', label: 'Spotpris', data: hours.map(h => data.prices[h]), backgroundColor: 'rgba(245, 158, 11, 0.4)', yAxisID: 'yP' },
            { type: 'line', label: 'Ack. Kostnad', data: rollData, borderColor: '#10b981', yAxisID: 'yC', pointRadius: 2, borderWidth: 3 }
          ]
        },
        options: { responsive: true, maintainAspectRatio: false, scales: { yP: { position: 'left' }, yC: { position: 'right', grid: { drawOnChartArea: false } } } }
      });
    }

    const ws = new WebSocket((location.protocol==='https:'?'wss':'ws')+'://'+location.host+'/ws');
    ws.onmessage = e => {
      const m = JSON.parse(e.data);
      document.getElementById('val-w').innerText = Math.round(m.active_power_w) + ' W';
      document.getElementById('val-a').innerText = m.active_current_l1_a.toFixed(1) + ' / ' + m.active_current_l2_a.toFixed(1) + ' / ' + m.active_current_l3_a.toFixed(1) + ' A';
      document.getElementById('val-v-multi').innerText = Math.round(m.voltage_l1_v) + ' / ' + Math.round(m.voltage_l2_v) + ' / ' + Math.round(m.voltage_l3_v) + ' V';
      document.getElementById('val-price').innerText = m.price_sek_kwh.toFixed(2) + ' kr';
      
      // Beräkna obalans
      const currents = [m.active_current_l1_a, m.active_current_l2_a, m.active_current_l3_a];
      const imbalance = Math.max(...currents) - Math.min(...currents);
      
      document.getElementById('phase-l1').innerText = m.active_current_l1_a.toFixed(1) + ' A';
      document.getElementById('phase-l2').innerText = m.active_current_l2_a.toFixed(1) + ' A';
      document.getElementById('phase-l3').innerText = m.active_current_l3_a.toFixed(1) + ' A';
      document.getElementById('total-a').innerText = 'Totalt: ' + m.total_current_a.toFixed(1) + ' A';
      
      const imbEl = document.getElementById('phase-imbalance');
      imbEl.innerText = imbalance.toFixed(1) + ' A';
      imbEl.style.color = imbalance > 10 ? '#ef4444' : (imbalance > 5 ? '#f59e0b' : '#10b981');

      if(pie) { pie.data.datasets[0].data = currents; pie.update('none'); }
    };

    window.onload = () => {
      applySavedTheme();
      document.getElementById('hDate').value = new Date().toISOString().split('T')[0];
      initChart(); loadHistory();
      pie = new Chart(document.getElementById('pie'), { type: 'doughnut', data: { labels: ['L1','L2','L3'], datasets: [{data:[0,0,0], backgroundColor:['#dc2626','#16a34a','#9333ea']}]}, options: {responsive:true, maintainAspectRatio:false}});
    };
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    init_db()
    threading.Thread(target=collector_loop, daemon=True).start()
    threading.Thread(target=elpris_scheduler, daemon=True).start()

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except:
        local_ip = "127.0.0.1"

    print(f" * Connecting to HomeWizard P1 at {P1_IP}...")
    print(" * Running on all addresses (0.0.0.0)")
    print(f" * Running on http://127.0.0.1:{PORT}")
    print(f" * Running on http://{local_ip}:{PORT}")
    print("Press CTRL+C to quit")

    app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False)