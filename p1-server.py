import sqlite3
import json
import time
import requests
from threading import Thread
from datetime import datetime
from flask import Flask, render_template_string
from flask_sock import Sock

# --- KONFIGURATION ---
P1_IP = "192.168.10.191"  
PHASE_LIMIT_A = 16        
DB_PATH = "p1.db"
POLL_INTERVAL = 10        

app = Flask(__name__)
sock = Sock(app)
clients = set()

# --- DATABAS-FUNKTIONER ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS measurements
                 (timestamp TEXT, power_w REAL, l1_a REAL, l2_a REAL, l3_a REAL, 
                  l1_v REAL, l2_v REAL, l3_v REAL)''')
    conn.commit()
    conn.close()

def save_to_db(data):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Vi använder .isoformat() för att undvika DeprecationWarning i Python 3.12+
    now = datetime.now().isoformat()
    c.execute("INSERT INTO measurements VALUES (?,?,?,?,?,?,?,?)",
              (now, data['active_power_w'], 
               data['active_current_l1_a'], data['active_current_l2_a'], data['active_current_l3_a'],
               data['active_voltage_l1_v'], data['active_voltage_l2_v'], data['active_voltage_l3_v']))
    conn.commit()
    conn.close()

# --- DATASYNC-LOOP ---
def fetch_data():
    while True:
        try:
            response = requests.get(f"http://{P1_IP}/api/v1/data", timeout=5)
            if response.status_code == 200:
                data = response.json()
                save_to_db(data)
                
                msg = json.dumps(data)
                for client in clients.copy():
                    try:
                        client.send(msg)
                    except:
                        clients.remove(client)
        except Exception as e:
            print(f"Fel vid hämtning: {e}")
        time.sleep(POLL_INTERVAL)

# --- WEBBSIDA ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>P1 Monitor Pro</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { font-family: sans-serif; background: #121212; color: white; padding: 20px; }
        .container { display: flex; flex-wrap: wrap; gap: 20px; }
        .card { background: #1e1e1e; padding: 20px; border-radius: 10px; flex: 1; min-width: 350px; }
        button { background: #28a745; color: white; border: none; padding: 10px 15px; border-radius: 5px; cursor: pointer; margin-top: 15px; }
        button:hover { background: #218838; }
    </style>
</head>
<body>
    <h1>⚡ P1 Monitor Pro</h1>
    <div class="container">
        <div class="card">
            <h2>Fasfördelning (A)</h2>
            <canvas id="pieChart"></canvas>
            <button onclick="downloadChart('pieChart', 'fasfordelning.png')">Spara som PNG</button>
        </div>
    </div>

    <script>
        const ctx = document.getElementById('pieChart').getContext('2d');
        const pieChart = new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels: ['L1', 'L2', 'L3'],
                datasets: [{ data: [0, 0, 0], backgroundColor: ['#ff6384', '#36a2eb', '#ffce56'] }]
            },
            options: { 
                plugins: { 
                    legend: { labels: { color: 'white', font: { size: 14 } } } 
                }
            }
        });

        function downloadChart(canvasId, filename) {
            const canvas = document.getElementById(canvasId);
            const tempCanvas = document.createElement('canvas');
            tempCanvas.width = canvas.width;
            tempCanvas.height = canvas.height;
            const tCtx = tempCanvas.getContext('2d');
            tCtx.fillStyle = 'white';
            tCtx.fillRect(0, 0, tempCanvas.width, tempCanvas.height);
            tCtx.drawImage(canvas, 0, 0);
            const link = document.createElement('a');
            link.download = filename;
            link.href = tempCanvas.toDataURL('image/png');
            link.click();
        }

        const socket = new WebSocket('ws://' + location.host + '/ws');
        socket.onmessage = function(event) {
            const data = JSON.parse(event.data);
            pieChart.data.datasets[0].data = [
                data.active_current_l1_a, 
                data.active_current_l2_a, 
                data.active_current_l3_a
            ];
            pieChart.update();
        };
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@sock.route('/ws')
def ws(ws):
    clients.add(ws)
    while True:
        data = ws.receive() # Håller anslutningen vid liv

if __name__ == '__main__':
    init_db()
    Thread(target=fetch_data, daemon=True).start()
    app.run(host='0.0.0.0', port=8000)