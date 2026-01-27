import requests
import json
from requests.exceptions import RequestException

P1_IP = "192.168.10.191"  # <-- din P1-meters IP

def get_p1_v1_data(ip: str) -> dict:
    url = f"http://{ip}/api/v1/data"
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        return resp.json()
    except RequestException as e:
        raise SystemExit(f"HTTP-fel: {e}")
    except ValueError:
        raise SystemExit("Kunde inte tolka JSON-svar.")

def fmt(value, decimals=1, unit=""):
    if value is None:
        return "—"
    try:
        s = f"{value:.{decimals}f}"
    except Exception:
        return "—"
    return f"{s}{(' ' + unit) if unit else ''}"

if __name__ == "__main__":
    data = get_p1_v1_data(P1_IP)

    # Läs ström per fas (A)
    i_l1 = data.get("active_current_l1_a")
    i_l2 = data.get("active_current_l2_a")
    i_l3 = data.get("active_current_l3_a")

    # Summera total ström om alla tre finns, annars summera det som finns
    currents = [v for v in (i_l1, i_l2, i_l3) if isinstance(v, (int, float))]
    total_current = sum(currents) if currents else None

    # Läs spänning per fas (V) – kan saknas på vissa mätare
    v_l1 = data.get("voltage_l1_v") or data.get("active_voltage_l1_v")
    v_l2 = data.get("voltage_l2_v") or data.get("active_voltage_l2_v")
    v_l3 = data.get("voltage_l3_v") or data.get("active_voltage_l3_v")

    phases = [
        ("Fas 1", v_l1, i_l1),
        ("Fas 2", v_l2, i_l2),
        ("Fas 3", v_l3, i_l3),
    ]

    # --- Viktigt: skriv ut INNE i loopen ---
    for name, v, i in phases:
        v_txt = fmt(v, 1)           # 1 decimal på volt
        i_txt = fmt(i, 2, "A")      # 2 decimaler + enhet A
        print(f"{name} (volt): {v_txt} ({i_txt})")

    # Övriga värden (kan heta olika i olika firmware)
    active_power_w = data.get("active_power_w")  # aktuell effekt i W
    # Vissa har total_import_kwh; andra har total_import_t1_kwh / t2 etc.
    total_import_kwh = (
        data.get("total_power_import_kwh")
        or data.get("total_import_kwh")
        or data.get("total_import_t1_kwh")
    )

    print(f"Aktuell effekt (W): {fmt(active_power_w, 0)}")
    print(f"Total ström (summa av faser): {fmt(total_current, 3, 'A')}")
    print(f"Total förbrukning (kWh): {fmt(total_import_kwh, 2)}")

    # Debug: om du vill se alla nycklar, avkommentera:
    # print(json.dumps(data, indent=2, ensure_ascii=False))