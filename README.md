# ⚡ P1 Monitor Pro + Pie

En komplett lösning för att övervaka din elförbrukning via en HomeWizard P1-mätare. Systemet loggar data lokalt till en SQLite-databas och presenterar insikter via en interaktiv webbpanel.

## ✨ Funktioner

* Live Dashboard: Realtidsuppdatering av effekt, ström och spänning.
* Fasfördelning: Cirkeldiagram som visar belastningen på L1, L2 och L3.
* Obalansanalys: Beräknar automatiskt snedbelastning mellan faserna.
* Excel-export: CSV-export optimerad för svenska inställningar.

## 📦 Python-moduler som krävs

Installera dessa via terminalen:
pip install Flask==3.0.0 flask-sock==0.7.0 requests==2.31.0

## 🛠 Installation som tjänst (Linux/systemd)

Följ dessa steg för att köra scriptet i bakgrunden:

1. Skapa service-filen:
   sudo nano /etc/systemd/system/p1monitor.service

2. Klistra in följande konfiguration i filen:

--------------------------------------------------
[Unit]
Description=P1 Monitor Pro Service
After=network.target

[Service]
User=pi
Group=pi
WorkingDirectory=/home/pi
ExecStart=/usr/bin/python3 /home/pi/p1-server.py
Restart=always
RestartSec=5
StandardOutput=inherit
StandardError=inherit

[Install]
WantedBy=multi-user.target
--------------------------------------------------



3. Aktivera tjänsten med dessa kommandon:
   sudo systemctl daemon-reload
   sudo systemctl enable p1monitor.service
   sudo systemctl start p1monitor.service

## 📊 Hantering

* Kontrollera status: sudo systemctl status p1monitor.service
* Se live-loggar: journalctl -u p1monitor.service -f
* Exportera data: Använd knappen i webbgränssnittet.

---
Projektet sparar all data lokalt i p1.db.