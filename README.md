# P1 Monitor ⚡

En lättviktig och robust energi-monitor för elmätare med P1-port. Systemet loggar data till en SQLite-databas, visar grafer i realtid via WebSockets och inkluderar funktioner för både analys, mörkerläge och export.

## ✨ Funktioner

* **Realtidsvisning:** Effekt (W), Ström (A) och Spänning (V) uppdateras live i 24H-format.
* **Fasfördelning:** Cirkeldiagram (Doughnut) som visar aktuell belastning mellan L1, L2 och L3.
* **Historik & Zoom:** Interaktiva grafer med stöd för zoom och panorering. Växla mellan 1h, 6h och 24h historik.
* **Mörkerläge:** Växla mellan ljust och mörkt tema via knapp. Valet sparas automatiskt i webbläsaren.
* **Dataexport:** * Exportera historik till **CSV** (semikolon-separerad med decimalkomma för Excel).
    * Spara den aktuella grafen som en **PNG-bild** (anpassas efter valt tema).
* **Gränsvärden:** Visar tydliga linjer för huvudsäkring (16A) och spänningsgränser.

## 🚀 Installation

1.  **Installera beroenden:**
    ```bash
    pip install flask flask-sock requests
    ```

2.  **Konfigurera IP-adress:**
    Ändra `P1_IP` i `p1-server.py` till IP-adressen för din P1-läsare.

3.  **Starta manuellt:**
    ```bash
    python p1-server.py
    ```

## 🔄 Köra som en tjänst (Linux/Raspberry Pi)

För att monitorn ska starta automatiskt vid boot och köras stabilt i bakgrunden bör du skapa en `systemd`-service.

1.  **Skapa filen:**
    ```bash
    sudo nano /etc/systemd/system/p1-monitor.service
    ```

2.  **Klistra in koden (justera sökvägar och användarnamn):**
    ```ini
    [Unit]
    Description=P1 Monitor Service
    After=network.target

    [Service]
    # Ersätt 'pi' med ditt faktiska användarnamn
    User=pi
    # Ersätt med den mapp där din fil ligger
    WorkingDirectory=/home/pi/p1-monitor
    ExecStart=/usr/bin/python3 p1-server.py
    Restart=always
    RestartSec=10

    [Install]
    WantedBy=multi-user.target
    ```

3.  **Aktivera tjänsten:**
    ```bash
    sudo systemctl daemon-reload
    sudo systemctl enable p1-monitor.service
    sudo systemctl start p1-monitor.service
    ```

4.  **Hantera tjänsten:**
    * **Status:** `sudo systemctl status p1-monitor.service`
    * **Stoppa:** `sudo systemctl stop p1-monitor.service`
    * **Loggar:** `journalctl -u p1-monitor.service -f`

## 📊 Databas
All data sparas i `p1.db` (SQLite). Databasen skapas automatiskt. Loggningsintervallet är som standard 10 sekunder för hög precision i realtidsvisningen.

---
*Logga din elförbrukning med stil.*