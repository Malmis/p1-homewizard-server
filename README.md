# P1 Monitor Pro ⚡

En avancerad realtidsmonitor för **HomeWizard P1 Wi-Fi Meter**. Systemet loggar din elförbrukning var 10:e sekund, hämtar spotpriser med kvartsupplösning och hjälper dig att optimera din fasbalans för att skydda dina huvudsäkringar.

## ✨ Huvudfunktioner

* **Realtidsvisning:** Effekt (W), Ström per fas (A), Spänning (V) och aktuellt spotpris via WebSockets.
* **Kvartsbaserade Elpriser:** Systemet hanterar den moderna prismodellen med unika priser per kvart för exakt kostnadsberäkning.
* **Fasbalans-modul:**
    * Grafiskt tårtdiagram över belastningen i realtid.
    * Beräkning av **Max obalans** (differensen mellan högsta och lägsta fas) med färgvarning (Grön/Gul/Röd).
* **Historik & Ekonomi:**
    * Beräknar faktiska kostnader baserat på din specifika förbrukning per timme/kvart.
    * Visar statistik för innevarande dygn och månad (kWh och SEK).
* **Permanent Lagring:**
    * **Mätdata:** Sparas i en lokal SQLite-databas (`p1.db`).
    * **Prisdatabas:** Hämtade spotpriser sparas permanent så att historik laddas direkt utan nya API-anrop.
* **Smart GUI:**
    * Interaktiva grafer med zoom och pan (Chart.js).
    * **Mörkt läge (Dark Mode):** Systemet sparar ditt temaval (state) i webbläsaren.
    * **Exportfunktioner:** Dedikerade knappar för Effekt-CSV, Effekt-PNG, All Data-CSV och Pris-PNG.

## 🚀 Installation

### 1. Förutsättningar
Du behöver Python 3 installerat. Installera nödvändiga bibliotek med:

    ```bash
    pip install flask flask-sock requests
    ```

### 2. Konfiguration
Öppna `p1-server.py` och kontrollera att variablerna i toppen av filen stämmer:

    ```python
    P1_IP = "192.168.2.141"  # IP-adressen till din HomeWizard P1
    ELOMRADE = "SE3"          # Ditt elområde (SE1, SE2, SE3 eller SE4)
    PORT = 8000               # Porten för webbgränssnittet
    ```

### 3. Starta manuellt
    ```bash
    python p1-server.py
    ```

Gå till `http://localhost:8000` i din webbläsare för att se din dashboard.

---

## 🐧 Kör som en tjänst i Linux (Ubuntu)

För att scriptet ska köras dygnet runt och starta automatiskt vid omstart, bör du sätta upp det som en `systemd`-tjänst.

1. **Skapa tjänstefilen:**
   ` ` `bash
   sudo nano /etc/systemd/system/p1monitor.service
   ` ` `

2. **Klistra in följande** (ersätt `dittnamn` och `/sökväg/till/mappen` med dina uppgifter):
   ` ` `ini
   [Unit]
   Description=P1 Monitor Pro Service
   After=network.target

   [Service]
   User=dittnamn
   WorkingDirectory=/home/dittnamn/p1-monitor
   ExecStart=/usr/bin/python3 /home/dittnamn/p1-monitor/p1-server.py
   Restart=always
   RestartSec=10

   [Install]
   WantedBy=multi-user.target
   ` ` `

3. **Aktivera tjänsten:**
   ` ` `bash
   sudo systemctl daemon-reload
   sudo systemctl enable p1monitor.service
   sudo systemctl start p1monitor.service
   ` ` `
---

## 🛠 Teknikstack

* **Backend:** Python 3 (Flask, Flask-Sock för WebSockets).
* **Databas:** SQLite 3 (Lokal lagring av mätvärden och priser).
* **Frontend:** Vanilla JS, CSS Variables, Chart.js 4.x.
* **Spotpriser:** Hämtas automatiskt från elprisetjustnu.se.

## 💡 Varför Fasbalans?
Håll ett öga på **Max obalans**. Om värdet ofta överstiger 10A kan det innebära att en av dina huvudsäkringar är kraftigt belastad medan de andra går tomma. Detta kan leda till att strömmen går trots att din totala förbrukning inte är för hög. Justera din belastning genom att flytta tunga förbrukare mellan faserna i elcentralen.

---
*Projektet är skapat för enkel energiövervakning i smarta hem.*