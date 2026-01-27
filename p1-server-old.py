#!/usr/bin/env python3
import json
import sqlite3
import threading
import queue
from datetime import datetime, timedelta, timezone
import time

import requests
from flask import (
    Flask,
    Response,
    request,
    jsonify,
    render_template_string,
    stream_with_context,
)
from flask_sock import Sock  # WebSocket-server

# =========================
# ---- Konfiguration ------
# =========================
P1_IP = "192.168.10.191"  # <-- BYT till din P1-meters IP (API v1)
POLL_INTERVAL = 10        # sekunder mellan mätningar
DB_PATH = "p1.db"
HOST = "0.0.0.0"
PORT = 8000

# Gränsvärden (anpassa efter din anläggning)
PHASE_LIMIT_A = 16  # huvudsäkring per fas (A)
VOLT_LOW = 207      # nedre spänningsgräns (V)
VOLT_HIGH = 253     # övre spänningsgräns (V)
VOLT_NOM = 230      # nominell spänning (V)

# =========================
# ---- Databaslager -------
# =========================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS p1_measurements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                measured_at TEXT NOT NULL,                 -- ISO 8601 UTC (Z)
                active_power_w REAL,
                total_import_kwh REAL,
                voltage_l1_v REAL,
                voltage_l2_v REAL,
                voltage_l3_v REAL,
                active_current_l1_a REAL,
                active_current_l2_a REAL,
                active_current_l3_a REAL
            )
        """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_measured_at ON p1_measurements(measured_at)"
        )
        conn.commit()
    finally:
        conn.close()


def insert_measurement(row: dict):
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO p1_measurements (
                measured_at, active_power_w, total_import_kwh,
                voltage_l1_v, voltage_l2_v, voltage_l3_v,
                active_current_l1_a, active_current_l2_a, active_current_l3_a
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                row["measured_at"],
                row.get("active_power_w"),
                row.get("total_import_kwh"),
                row.get("voltage_l1_v"),
                row.get("voltage_l2_v"),
                row.get("voltage_l3_v"),
                row.get("active_current_l1_a"),
                row.get("active_current_l2_a"),
                row.get("active_current_l3_a"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _row_to_point_dict(r: sqlite3.Row) -> dict:
    """DB-rad -> dict + numeriska typer + obalans."""
    p = dict(r)
    for k in (
        "active_power_w",
        "total_current_a",
        "active_current_l1_a",
        "active_current_l2_a",
        "active_current_l3_a",
        "voltage_l1_v",
        "voltage_l2_v",
        "voltage_l3_v",
    ):
        if k in p and p[k] is not None:
            p[k] = float(p[k])
    # Obalans
    i_vals = [
        p.get("active_current_l1_a"),
        p.get("active_current_l2_a"),
        p.get("active_current_l3_a"),
    ]
    if all(v is not None for v in i_vals):
        i_max = max(i_vals)
        i_min = min(i_vals)
        ob_a = i_max - i_min
        p["imbalance_a"] = ob_a
        p["imbalance_pct"] = (ob_a / i_max * 100.0) if i_max > 0 else 0.0
    else:
        p["imbalance_a"] = None
        p["imbalance_pct"] = None
    return p


def query_series(from_dt, aggregate_minute: bool):
    """
    Hämtar tidsserie från 'from_dt' till nu.
    aggregate_minute=True => medel per minut (mindre payload).
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        start_iso = (
            from_dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        )
        if aggregate_minute:
            cur.execute(
                """
                SELECT
                  strftime('%Y-%m-%dT%H:%M:00Z', measured_at) AS ts,
                  AVG(active_power_w) AS active_power_w,
                  AVG(COALESCE(active_current_l1_a,0) + COALESCE(active_current_l2_a,0) + COALESCE(active_current_l3_a,0)) AS total_current_a,
                  AVG(active_current_l1_a) AS active_current_l1_a,
                  AVG(active_current_l2_a) AS active_current_l2_a,
                  AVG(active_current_l3_a) AS active_current_l3_a,
                  AVG(voltage_l1_v) AS voltage_l1_v,
                  AVG(voltage_l2_v) AS voltage_l2_v,
                  AVG(voltage_l3_v) AS voltage_l3_v
                FROM p1_measurements
                WHERE measured_at >= ?
                GROUP BY ts
                ORDER BY ts ASC
            """,
                (start_iso,),
            )
        else:
            cur.execute(
                """
                SELECT
                  measured_at AS ts,
                  active_power_w,
                  (COALESCE(active_current_l1_a,0) + COALESCE(active_current_l2_a,0) + COALESCE(active_current_l3_a,0)) AS total_current_a,
                  active_current_l1_a, active_current_l2_a, active_current_l3_a,
                  voltage_l1_v, voltage_l2_v, voltage_l3_v
                FROM p1_measurements
                WHERE measured_at >= ?
                ORDER BY ts ASC
            """,
                (start_iso,),
            )
        rows = cur.fetchall()
        return [_row_to_point_dict(r) for r in rows]
    finally:
        conn.close()


def query_latest():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
              measured_at AS ts,
              active_power_w,
              (COALESCE(active_current_l1_a,0) + COALESCE(active_current_l2_a,0) + COALESCE(active_current_l3_a,0)) AS total_current_a,
              active_current_l1_a, active_current_l2_a, active_current_l3_a,
              voltage_l1_v, voltage_l2_v, voltage_l3_v
            FROM p1_measurements
            ORDER BY measured_at DESC, id DESC
            LIMIT 1
        """
        )
        row = cur.fetchone()
        return _row_to_point_dict(row) if row else None
    finally:
        conn.close()


# =========================
# ---- P1-hämtning --------
# =========================
def fetch_p1_v1(ip: str) -> dict:
    url = f"http://{ip}/api/v1/data"
    resp = requests.get(url, timeout=5)
    resp.raise_for_status()
    return resp.json()


# =========================
# ---- WebSocket push -----
# =========================
subscribers = set()  # en Queue per ansluten klient
subs_lock = threading.Lock()


def broadcast(msg: dict):
    """Skicka till alla aktiva WebSocket-prenumeranter (icke-blockerande)."""
    with subs_lock:
        dead = []
        for q in list(subscribers):
            try:
                q.put_nowait(msg)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(msg)
                except Exception:
                    dead.append(q)
        for q in dead:
            subscribers.discard(q)


def collector_loop():
    while True:
        try:
            data = fetch_p1_v1(P1_IP)
            now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

            row = {
                "measured_at": now_iso,
                "active_power_w": data.get("active_power_w"),
                "total_import_kwh": data.get("total_power_import_kwh")
                or data.get("total_import_kwh")
                or data.get("total_import_t1_kwh"),
                "voltage_l1_v": data.get("voltage_l1_v")
                or data.get("active_voltage_l1_v"),
                "voltage_l2_v": data.get("voltage_l2_v")
                or data.get("active_voltage_l2_v"),
                "voltage_l3_v": data.get("voltage_l3_v")
                or data.get("active_voltage_l3_v"),
                "active_current_l1_a": data.get("active_current_l1_a"),
                "active_current_l2_a": data.get("active_current_l2_a"),
                "active_current_l3_a": data.get("active_current_l3_a"),
            }
            insert_measurement(row)

            latest = {
                "ts": now_iso,
                "active_power_w": row.get("active_power_w"),
                "active_current_l1_a": row.get("active_current_l1_a"),
                "active_current_l2_a": row.get("active_current_l2_a"),
                "active_current_l3_a": row.get("active_current_l3_a"),
                "total_current_a": sum(
                    [
                        v
                        for v in [
                            row.get("active_current_l1_a"),
                            row.get("active_current_l2_a"),
                            row.get("active_current_l3_a"),
                        ]
                        if isinstance(v, (int, float))
                    ]
                ),
                "voltage_l1_v": row.get("voltage_l1_v"),
                "voltage_l2_v": row.get("voltage_l2_v"),
                "voltage_l3_v": row.get("voltage_l3_v"),
            }
            # obalans
            i_vals = [
                latest.get("active_current_l1_a"),
                latest.get("active_current_l2_a"),
                latest.get("active_current_l3_a"),
            ]
            if all(isinstance(v, (int, float)) for v in i_vals):
                i_max = max(i_vals)
                i_min = min(i_vals)
                ob_a = i_max - i_min
                latest["imbalance_a"] = ob_a
                latest["imbalance_pct"] = (ob_a / i_max * 100.0) if i_max > 0 else 0.0
            else:
                latest["imbalance_a"] = None
                latest["imbalance_pct"] = None

            broadcast(latest)
        except Exception as e:
            print("Collector error:", e)
        time.sleep(POLL_INTERVAL)


# =========================
# ---- Flask-app ----------
# =========================
app = Flask(__name__)
sock = Sock(app)

# ---------- favicon (slippa 404) ----------
@app.route("/favicon.ico")
def favicon():
    return ("", 204)


# ---------- HTML ----------
INDEX_TEMPLATE = r"""<!doctype html>
<html lang="sv">
<head>
  <meta charset="utf-8">
  <title>P1 – Live & historik (WebSocket + Zoom)</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">

  <!-- KORREKTA script-taggar med src (rätt ordning) -->
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <!-- chartjs-adapter-date-fns@3 behöver date-fns v3 som global -->
  <script src="https://cdn.jsdelivr.net/npm/date-fns@3.6.0/dist/date-fns.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
  <!-- Zoom-plugin (UMD) -->
  <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.umd.min.js"></script>

  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 1rem; color:#222; }
    h1 { margin: .2rem 0 .6rem; }
    .controls { display:flex; gap:.5rem; margin:.5rem 0 1rem; flex-wrap: wrap; }
    button, a.btn { padding:.4rem .75rem; border:1px solid #bbb; border-radius:6px; background:#f7f7f7; cursor:pointer; text-decoration:none; color:#222; }
    button.active { background:#e7f0ff; border-color:#6aa1ff; }
    .wrap { max-width: 1100px; }
    .meta { color:#666; font-size:.9rem }
    .stats { margin-top: .75rem; font-size: .95rem; color:#222; }
    .stats strong { font-weight: 600; }
    .good { color: #1a7f37; }
    .warn { color: #a15b00; }
    .bad  { color: #c62828; }
    .export { margin:.5rem 0 1rem; display:flex; gap:.5rem; flex-wrap:wrap; }
    /* Gör att canvas prioriterar egna gesture-events */
    #chart { touch-action: none; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>HomeWizard P1 – Live & historik</h1>

    <!-- injicerar Python-konfig som JS-objekt -->
    <script>
      window.P1CFG = {
        poll: {{ poll_interval|int }},
        voltLow: {{ volt_low|float }},
        voltHigh: {{ volt_high|float }},
        voltNom: {{ volt_nom|float }},
        phaseLimitA: {{ phase_limit_a|float }}
      };
    </script>

    <div class="controls">
      <button data-hours="1" class="active">1 h</button>
      <button data-hours="6">6 h</button>
      <button data-hours="24">24 h</button>
      <button data-hours="168">7 d</button>
      <button id="btnReset">Återställ zoom</button>
    </div>

    <!-- Snabba exportlänkar (Excelvänligt: ; + komma) -->
    <div class="export">
      <a class="btn" href="/api/export.csv?hours=1&sep=%3B&decimal=comma">Exportera 1 h (CSV)</a>
      <a class="btn" href="/api/export.csv?hours=24&sep=%3B&decimal=comma">Exportera 24 h (CSV)</a>
      <a class="btn" href="/api/export.csv?hours=168&sep=%3B&decimal=comma">Exportera 7 d (CSV)</a>
    </div>

    <canvas id="chart" height="120"></canvas>
    <div class="stats" id="stats"></div>
    <p class="meta"><small>
      Zooma: <strong>mushjul</strong> eller <strong>dra‑ruta</strong> (vänsterknapp).
      Panorera: <strong>Shift + dra</strong> eller <strong>mittknapp + dra</strong>.
      Dubbelklick eller <em>Återställ zoom</em> nollställer. Realtidsuppdatering via WebSocket (var {{ poll_interval }} s).
    </small></p>
  </div>

  {% raw %}
  <script>
  // ---- Robust registrering av zoom-plugin ----
  (function ensureZoomRegistered() {
    const Zoom = window.ChartZoom || window.chartjsPluginZoom || window['chartjs-plugin-zoom'];
    if (typeof Chart === 'undefined') {
      console.error('[P1] Chart.js saknas – kontrollera <script src> för chart.umd.min.js');
      return;
    }
    if (!Zoom || typeof Zoom !== 'object') {
      console.error('[P1] zoom-plugin saknas – kontrollera <script src> för chartjs-plugin-zoom.umd.min.js');
      return;
    }
    try {
      Chart.register(Zoom);
      console.log('[P1] Zoom-plugin registrerad:', Zoom.id || Zoom);
    } catch (e) {
      console.error('[P1] Kunde inte registrera zoom-plugin:', e);
    }
  })();

  const $buttons = Array.from(document.querySelectorAll('button[data-hours]'));
  const $btnReset = document.getElementById('btnReset');
  const $stats   = document.getElementById('stats');
  const $canvas  = document.getElementById('chart');
  let chart, lastTs = null, currentHours = 1, ws;

  // VIKTIGT: stoppa sid-zoom (Ctrl + mushjul) över canvasen så att diagrammet får hjul-eventet
  $canvas.addEventListener('wheel', (e) => {
    if (e.ctrlKey) { e.preventDefault(); }   // kräver {passive:false}, vilket most browsers använder på addEventListener för 'wheel'
  }, { passive: false });

  // Svensk formattering
  const fmtTickShort = new Intl.DateTimeFormat('sv-SE', { hour: '2-digit', minute:'2-digit' });
  const fmtTickLong  = new Intl.DateTimeFormat('sv-SE', { year: 'numeric', month:'short', day:'2-digit', hour: '2-digit', minute:'2-digit' });
  const fmtTooltip   = new Intl.DateTimeFormat('sv-SE', { year: 'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit' });

  function timeLabel(d, spanHours) {
    return (spanHours >= 24) ? fmtTickLong.format(d) : fmtTickShort.format(d);
  }

  async function fetchSeries(hours=1) {
    const res = await fetch(`/api/series?hours=${encodeURIComponent(hours)}`);
    if (!res.ok) throw new Error('Kunde inte hämta data');
    return await res.json();
  }

  function mkDataset({label, color, yAxis, dash=false, fill=false}) {
    return {
      label,
      data: [],
      borderWidth: 1.7,
      borderColor: color,
      backgroundColor: fill ? color : (color + '33'),
      pointRadius: 0,
      tension: 0.15,
      spanGaps: true,
      yAxisID: yAxis,
      borderDash: dash ? [6, 4] : undefined,
      fill: fill
    };
  }

  function buildEmptySeries() {
    return [
      mkDataset({ label: 'Effekt (W)',         color: '#1f77b4', yAxis: 'yW' }),
      mkDataset({ label: 'Total ström (A)',    color: '#ff7f0e', yAxis: 'yA' }),
      mkDataset({ label: 'L1 ström (A)',       color: '#d62728', yAxis: 'yA' }),
      mkDataset({ label: 'L2 ström (A)',       color: '#2ca02c', yAxis: 'yA' }),
      mkDataset({ label: 'L3 ström (A)',       color: '#9467bd', yAxis: 'yA' }),
      mkDataset({ label: 'L1 spänning (V)',    color: '#17becf', yAxis: 'yV', dash: true }),
      mkDataset({ label: 'L2 spänning (V)',    color: '#8c564b', yAxis: 'yV', dash: true }),
      mkDataset({ label: 'L3 spänning (V)',    color: '#7f7f7f', yAxis: 'yV', dash: true }),
      mkDataset({ label: 'Obalans (A)',        color: '#111111', yAxis: 'yA' })
    ];
  }

  function addPointSeries(ds, p) {
    const x = new Date(p.ts);
    ds[0].data.push({ x, y: p.active_power_w });
    ds[1].data.push({ x, y: p.total_current_a });
    ds[2].data.push({ x, y: p.active_current_l1_a });
    ds[3].data.push({ x, y: p.active_current_l2_a });
    ds[4].data.push({ x, y: p.active_current_l3_a });
    ds[5].data.push({ x, y: p.voltage_l1_v });
    ds[6].data.push({ x, y: p.voltage_l2_v });
    ds[7].data.push({ x, y: p.voltage_l3_v });
    ds[8].data.push({ x, y: p.imbalance_a });
  }

  // --- Gränsvärden: horisontella linjer + spänningsband ---
  function hLine(label, y, axis, color, dash=false) {
    return {
      label,
      data: [{x:0,y:y},{x:0,y:y}], // x uppdateras efter skala
      borderColor: color,
      backgroundColor: color + '22',
      borderWidth: 1.5,
      borderDash: dash ? [6,4] : undefined,
      pointRadius: 0,
      yAxisID: axis,
      tension: 0,
      parsing: false,
      fill: false
    };
  }

  function buildThresholds(startX, endX) {
    const CL = window.P1CFG.phaseLimitA;
    const VN = window.P1CFG.voltNom;
    const VL = window.P1CFG.voltLow;
    const VH = window.P1CFG.voltHigh;

    const aLimit = hLine(`Fasgräns ${CL} A`, CL, 'yA', '#e53935', true);
    const vLow  = hLine(`Spänning Låg (${VL} V)`, VL, 'yV', '#9e9e9e', true);
    const vHigh = hLine(`Spänning Hög (${VH} V)`, VH, 'yV', '#9e9e9e', true);
    const vNom  = hLine(`Nominell (${VN} V)`,     VN, 'yV', '#616161', true);

    for (const d of [aLimit, vLow, vHigh, vNom]) { d.data[0].x = startX; d.data[1].x = endX; }
    vHigh.fill = '-1'; vHigh.backgroundColor = '#9e9e9e22';
    return [vLow, vHigh, vNom, aLimit]; // ordning för fill (vHigh fyller mot föregående)
  }

  function adjustThresholdsToView(chart) {
    const x = chart.scales.x;
    if (!x) return;
    const min = new Date(x.min), max = new Date(x.max);
    const prefixes = ['Fasgräns', 'Spänning Låg', 'Spänning Hög', 'Nominell'];
    chart.data.datasets.forEach(ds => {
      if (!ds || typeof ds.label !== 'string') return;
      if (prefixes.some(prefix => ds.label.startsWith(prefix))) {
        if (ds.data && ds.data.length >= 2) { ds.data[0].x = min; ds.data[1].x = max; }
      }
    });
    chart.update('none');
  }

  function renderStats(latest) {
    const $s = $stats;
    if (!latest) { $s.textContent = ''; return; }
    const i1 = latest.active_current_l1_a, i2 = latest.active_current_l2_a, i3 = latest.active_current_l3_a;
    const obA = latest.imbalance_a, obPct = latest.imbalance_pct;
    const v1 = latest.voltage_l1_v, v2 = latest.voltage_l2_v, v3 = latest.voltage_l3_v;
    const ts = new Date(latest.ts);

    let cls = 'good'; if (obPct >= 20) cls = 'bad'; else if (obPct >= 10) cls = 'warn';
    $s.innerHTML = `
      <div><strong>${new Intl.DateTimeFormat('sv-SE', {
        year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit'
      }).format(ts)}</strong></div>
      <div>Fasströmmar: L1 <strong>${(i1??0).toLocaleString('sv-SE',{maximumFractionDigits:2})} A</strong>,
                       L2 <strong>${(i2??0).toLocaleString('sv-SE',{maximumFractionDigits:2})} A</strong>,
                       L3 <strong>${(i3??0).toLocaleString('sv-SE',{maximumFractionDigits:2})} A</strong></div>
      <div>Spänning:    L1 <strong>${(v1??0).toLocaleString('sv-SE',{maximumFractionDigits:1})} V</strong>,
                       L2 <strong>${(v2??0).toLocaleString('sv-SE',{maximumFractionDigits:1})} V</strong>,
                       L3 <strong>${(v3??0).toLocaleString('sv-SE',{maximumFractionDigits:1})} V</strong></div>
      <div>Obalans: <strong class="${cls}">${(obA??0).toLocaleString('sv-SE',{maximumFractionDigits:2})} A</strong>
                    (<strong class="${cls}">${(obPct??0).toLocaleString('sv-SE',{maximumFractionDigits:1})}%</strong>)</div>`;
  }

  async function render(hours=1) {
    currentHours = hours;
    $buttons.forEach(b => b.classList.toggle('active', +b.dataset.hours === +hours));

    // Hämta historik (kan vara tom de första sekunderna)
    let points = [];
    try { points = (await fetchSeries(hours)).points || []; } catch(e){ console.warn(e); }

    // Bygg datasets (tomma eller med historik)
    const series = buildEmptySeries();
    if (points.length) {
      lastTs = points[points.length - 1].ts;
      for (const p of points) addPointSeries(series, p);
    }

    // Trösklar: om vi saknar historik, utgå från nu-intervallet
    const now = new Date();
    const defaultStart = new Date(now.getTime() - hours*60*60*1000);
    const startX = points.length ? new Date(points[0].ts) : defaultStart;
    const endX   = points.length ? new Date(points[points.length-1].ts) : now;
    const thresholds = buildThresholds(startX, endX);
    const datasets = [...series, ...thresholds];

    // Skapa eller återskapa grafen
    const ctx = document.getElementById('chart').getContext('2d');
    if (chart) chart.destroy();
    chart = new Chart(ctx, {
      type: 'line',
      data: { datasets },
      options: {
        animation: false, parsing: false,
        scales: {
          x: {
            type: 'time',
            time: { tooltipFormat: 'yyyy-MM-dd HH:mm:ss' },
            ticks: { maxRotation: 0, autoSkip: true,
              callback: (val, idx, ticks) => timeLabel(new Date(ticks[idx].value), currentHours) }
          },
          yW: { type: 'linear', position: 'left',  title: { display: true, text: 'W' }, grid: { drawOnChartArea: true  }},
          yA: { type: 'linear', position: 'right', title: { display: true, text: 'A' }, grid: { drawOnChartArea: false }},
          yV: { type: 'linear', position: 'right', title: { display: true, text: 'V' }, offset: true, grid: { drawOnChartArea: false } }
        },
        plugins: {
          legend: { display: true },
          tooltip: {
            mode: 'nearest', intersect: false,
            callbacks: {
              title: (items) => items.length ? fmtTooltip.format(new Date(items[0].parsed.x)) : '',
              label: (ctx) => {
                const v = ctx.parsed.y;
                let unit = ctx.dataset.yAxisID==='yW' ? ' W' : (ctx.dataset.yAxisID==='yA' ? ' A' : ' V');
                return `${ctx.dataset.label}: ${(v ?? '').toLocaleString('sv-SE',{maximumFractionDigits:2})}${unit}`;
              }
            }
          },
          // ---- ZOOM/PAN (uppdaterad) ----
          zoom: {
            limits: { x: { minRange: 5 * 1000 } }, // minst 5 sek för att lätt kunna zooma
            pan: {
              enabled: true,
              mode: 'x',
              modifierKey: 'shift',
              overScaleMode: 'x',
              mouseButton: 'middle'
            },
            zoom: {
              // 1) Mushjul-zoom utan modifierare
              wheel: { enabled: true },

              // 2) Nyp-zoom (touch)
              pinch: { enabled: true },

              // 3) Dra-ruta (vänsterknapp)
              drag: {
                enabled: true,
                backgroundColor: 'rgba(100, 149, 237, 0.15)',
                borderColor: 'rgba(100, 149, 237, 0.8)',
                borderWidth: 1
              },

              mode: 'x'
            },
            onZoomComplete: ({chart}) => adjustThresholdsToView(chart),
            onPanComplete:  ({chart}) => adjustThresholdsToView(chart)
          }
        }
      }
    });

    // dubbelklick = reset zoom
    document.getElementById('chart').ondblclick = () => { chart.resetZoom(); adjustThresholdsToView(chart); };
    $btnReset.onclick = () => { chart.resetZoom(); adjustThresholdsToView(chart); };

    // Visa status om vi redan har en punkt
    renderStats(points.length ? points[points.length-1] : null);

    // Starta WS även om historiken var tom
    connectWS();
  }

  function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}/ws`;
    if (ws) try { ws.close(); } catch (e) { }
    ws = new WebSocket(url);

    ws.onopen = () => console.log('WS: ansluten');
    ws.onclose = () => { console.log('WS: stängd, försöker igen om 2s'); setTimeout(connectWS, 2000); };
    ws.onerror = (e) => console.warn('WS-fel', e);
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (!msg || !chart) return;

        // Lägg till ny punkt
        lastTs = msg.ts;
        const x = new Date(msg.ts);
        const ds = chart.data.datasets;

        // Ordning: [ Effekt, TotalA, L1A, L2A, L3A, L1V, L2V, L3V, ObalansA, ...thresholds ]
        ds[0].data.push({ x, y: msg.active_power_w });
        ds[1].data.push({ x, y: msg.total_current_a });
        ds[2].data.push({ x, y: msg.active_current_l1_a });
        ds[3].data.push({ x, y: msg.active_current_l2_a });
        ds[4].data.push({ x, y: msg.active_current_l3_a });
        ds[5].data.push({ x, y: msg.voltage_l1_v });
        ds[6].data.push({ x, y: msg.voltage_l2_v });
        ds[7].data.push({ x, y: msg.voltage_l3_v });
        ds[8].data.push({ x, y: msg.imbalance_a });

        // Förläng tröskellinjernas slut-X till senaste x
        const thrStartIndex = 9;
        ds.slice(thrStartIndex).forEach(d => { if (d.data && d.data.length >= 2) d.data[1].x = x; });

        chart.update('none');
        renderStats(msg);
      } catch (e) {
        console.warn('WS message parse error', e);
      }
    };
  }

  $buttons.forEach(btn => btn.addEventListener('click', () => render(+btn.dataset.hours)));
  render(1);
  </script>
  {% endraw %}
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(
        INDEX_TEMPLATE,
        poll_interval=POLL_INTERVAL,
        phase_limit_a=PHASE_LIMIT_A,
        volt_low=VOLT_LOW,
        volt_high=VOLT_HIGH,
        volt_nom=VOLT_NOM,
    )


@app.route("/api/series")
def api_series():
    try:
        hours = int(request.args.get("hours", "1"))
    except ValueError:
        hours = 1
    hours = max(1, min(hours, 24 * 31))  # max 31 dygn

    now = datetime.now(timezone.utc)
    from_dt = now - timedelta(hours=hours)
    aggregate = hours > 6  # aggregera per minut vid längre perioder
    points = query_series(from_dt, aggregate)
    return jsonify({"from": from_dt.isoformat(), "to": now.isoformat(), "points": points})


# ---------- CSV EXPORT ----------
@app.route("/api/export.csv")
def api_export_csv():
    """
    Parametrar:
      - hours=N  (alternativt from=ISO8601&to=ISO8601)
      - aggregate=raw|minute   (default: minute om hours>6 annars raw)
      - sep=','|';'|'\t'      (default: ',')
      - decimal=dot|comma     (default: dot)
      - cols=csv_lista        (default: se code)
      - tz=utc|stockholm      (default: utc)
      - timefmt=iso|sv        (default: iso)
    """
    now = datetime.now(timezone.utc)
    p_from = request.args.get("from")
    p_to = request.args.get("to")
    hours = request.args.get("hours", type=int)
    if p_from:
        try:
            from_dt = (
                datetime.fromisoformat(p_from.replace("Z", "+00:00"))
                if p_from.endswith("Z")
                else datetime.fromisoformat(p_from)
            )
        except Exception:
            return Response("Ogiltigt 'from' (ISO8601)", status=400)
    else:
        if not hours:
            hours = 24
        hours = max(1, min(hours, 24 * 31))
        from_dt = now - timedelta(hours=hours)

    if p_to:
        try:
            to_dt = (
                datetime.fromisoformat(p_to.replace("Z", "+00:00"))
                if p_to.endswith("Z")
                else datetime.fromisoformat(p_to)
            )
        except Exception:
            return Response("Ogiltigt 'to' (ISO8601)", status=400)
    else:
        to_dt = now

    aggregate_param = (request.args.get("aggregate") or "").lower()
    if aggregate_param in ("raw", "minute"):
        aggregate = aggregate_param == "minute"
    else:
        aggregate = hours and hours > 6

    default_cols = [
        "ts",
        "active_power_w",
        "total_current_a",
        "active_current_l1_a",
        "active_current_l2_a",
        "active_current_l3_a",
        "voltage_l1_v",
        "voltage_l2_v",
        "voltage_l3_v",
        "imbalance_a",
        "imbalance_pct",
    ]
    cols = request.args.get("cols")
    columns = [c.strip() for c in cols.split(",") if c.strip()] if cols else default_cols

    sep = request.args.get("sep", ",")
    if sep == "\\t":
        sep = "\t"
    decimal = (request.args.get("decimal", "dot").lower())
    decimal_comma = decimal == "comma"
    tz = (request.args.get("tz", "utc").lower())
    timefmt = (request.args.get("timefmt", "iso").lower())

    points_all = query_series(from_dt, aggregate)
    points = [
        p
        for p in points_all
        if datetime.fromisoformat(p["ts"].replace("Z", "+00:00")) <= to_dt
    ]

    def format_ts(ts_iso):
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        if tz == "stockholm":
            try:
                from zoneinfo import ZoneInfo
                dt = dt.astimezone(ZoneInfo("Europe/Stockholm"))
            except Exception:
                pass
        if timefmt == "sv":
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        return dt.replace(microsecond=0).isoformat()

    def fmt_num(v):
        if v is None:
            return ""
        if isinstance(v, (int, float)):
            s = f"{v:.6f}".rstrip("0").rstrip(".")
            if decimal_comma:
                s = s.replace(".", ",")
            return s
        return str(v)

    filename = f"p1_export_{from_dt.strftime('%Y%m%dT%H%M%S')}_{to_dt.strftime('%Y%m%dT%H%M%S')}.csv"
    headers = {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": f'attachment; filename="{filename}"',
    }

    def generate():
        yield sep.join(columns) + "\n"
        for p in points:
            row = []
            for c in columns:
                if c == "ts":
                    row.append(format_ts(p.get("ts")))
                else:
                    row.append(fmt_num(p.get(c)))
            yield sep.join(row) + "\n"

    return Response(stream_with_context(generate()), headers=headers)


# ---------- WebSocket ----------
@sock.route("/ws")
def ws_route(ws):
    q = queue.Queue(maxsize=200)
    with subs_lock:
        subscribers.add(q)
    try:
        latest = query_latest()
        if latest:
            ws.send(json.dumps(latest))
        while True:
            msg = q.get()
            ws.send(json.dumps(msg))
    except Exception:
        pass
    finally:
        with subs_lock:
            subscribers.discard(q)


# =========================
# ---- Entrypoint ---------
# =========================
if __name__ == "__main__":
    init_db()
    t = threading.Thread(target=collector_loop, daemon=True)
    t.start()
    print(
        f"Servern kör på http://{HOST}:{PORT}  (P1: {POLL_INTERVAL}s intervall, IP: {P1_IP})"
    )
    app.run(host=HOST, port=PORT, threaded=True)