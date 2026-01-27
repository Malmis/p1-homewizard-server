# ⚡ Energimonitor P1

En kraftfull monitor för din HomeWizard P1-mätare. Systemet loggar elförbrukning i en lokal SQLite-databas och visar realtidsdata via en webbpanel.

## ✨ Funktioner

* Realtidsövervakning: Se effekt (W), ström (A) och spänning (V) live.
* Historik & Zoom: Utforska data för 1h, 6h, 24h eller 7 dagar.
* Obalansvarning: Beräknar snedbelastning mellan faser (L1, L2, L3).
* Gränsvärden: Visuella linjer för huvudsäkring och spänningsnivåer.
* Excel-export: CSV-export optimerad för svenska Excel-inställningar.

## 🚀 Installation

### 1. Förutsättningar
Du behöver ha Python 3.7+ installerat på din dator eller Raspberry Pi.

### 2. Installera bibliotek
Öppna din terminal och kör följande kommando:
pip install Flask==3.0.0 flask-sock==0.7.0 requests==2.31.0

### 3. Konfiguration
Öppna p1-server.py och kontrollera dessa variabler längst upp i filen:
- P1_IP: Ange IP-adressen till din HomeWizard P1-mätare.
- PHASE_LIMIT_A: Ange storleken på din huvudsäkring (t.ex. 16, 20 eller 25).

## 🛠 Användning

1. Starta scriptet:
   python p1-server.py

2. Öppna webbläsaren:
   Gå till http://localhost:8000 (eller den IP-adress som visas i terminalen).

## 📊 Data och Export

* Databas: All data lagras i filen p1.db.
* Export: Klicka på "Exportera CSV" i webbgränssnittet för att ladda ner historik. Filen använder semikolon som separator för att fungera direkt i svenska Excel.

---
Projektet körs helt lokalt och skickar ingen data till externa molntjänster.