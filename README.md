# ⚡ P1 Monitor Pro

Lokal övervakning av HomeWizard P1-mätare med realtidsgrafer och PNG-export.

## 📦 Installation
Kör följande kommando för att installera nödvändiga moduler:

pip install Flask==3.0.0 flask-sock==0.7.0 requests==2.31.0

## 🚀 Snabbstart
1. Öppna p1-server.py och sätt rätt P1_IP.
2. Starta med: python p1-server.py
3. Gå till: http://localhost:8000

## 🖼 PNG-Export
I webbläsaren finns nu en knapp under varje graf. När du klickar på den skapas en PNG-bild med vit bakgrund som sparas på din dator. Perfekt för dokumentation av din elförbrukning!

## 🛠 Linux Service (Autostart)
För att köra detta som en tjänst på t.ex. Raspberry Pi:

1. sudo nano /etc/systemd/system/p1monitor.service
2. Klistra in följande:

```
[Unit]
Description=P1 Monitor Service
After=network.target

[Service]
User=pi
WorkingDirectory=/home/pi
ExecStart=/usr/bin/python3 /home/pi/p1-server.py
Restart=always

[Install]
WantedBy=multi-user.target
```

3. Kör: sudo systemctl daemon-reload && sudo systemctl enable p1monitor.service && sudo systemctl start p1monitor.service

## 📊 Tekniker som används
- Flask & Flask-Sock: Webserver och realtidsströmning.
- Chart.js: Visualisering av data.
- SQLite: Lokal lagring utan molnkrav.