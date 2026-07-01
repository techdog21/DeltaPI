"""
DeltaPi — battery / solar energy model.

The domain brain: LiFePO4 SOC estimation from resting voltage, the SOC pill, the
forward-looking Sustainability Outlook, the empirical house-load estimate (since
the MPPT can't measure load), and the voltage chart series.
"""
import json
from datetime import datetime, timedelta

from config import BATTERY_CAPACITY_WH, SOC_FLOOR, LOAD_WINDOW_HOURS
from util import clean_int, ensure_utc, fmt_mt


def estimate_soc(voltage):
    """
    Estimates State of Charge (%) for LiFePO4 batteries based on resting voltage.
    Only accurate when battery is not under heavy load or active charging.
    """
    if voltage >= 13.6: return 100
    elif voltage >= 13.4: return 95
    elif voltage >= 13.2: return 90
    elif voltage >= 13.1: return 85
    elif voltage >= 13.0: return 75
    elif voltage >= 12.9: return 65
    elif voltage >= 12.8: return 55
    elif voltage >= 12.7: return 45
    elif voltage >= 12.6: return 35
    elif voltage >= 12.4: return 25
    elif voltage >= 12.2: return 15
    else: return 5


def soc_pill(soc):
    """Returns (color_class, label) pill for a given SOC percentage."""
    if soc >= 80: return ("green", "High")
    elif soc >= 50: return ("yellow", "Medium")
    else: return ("red", "Low")


def sustainability_outlook(batt_present, soc, charging_now, usable_wh,
                           avg_harvest_wh, avg_cons_wh, forecast_tier):
    """Forward-looking energy state for the Solar panel, fusing three horizons:
    now (is the battery charging?), multi-day (measured daily harvest vs
    consumption), and the solar forecast (can the sun keep up?). Returns
    (pill_class, label)."""
    if not batt_present:
        return ("gray", "—")
    poor = forecast_tier == "poor"
    good = forecast_tier == "good"
    have_bal = avg_harvest_wh is not None and avg_cons_wh and avg_cons_wh > 0
    balance = (avg_harvest_wh - avg_cons_wh) if have_bal else None  # Wh/day, + = surplus

    # Low battery with no sun coming = the one hard countdown.
    if soc is not None and soc <= 25 and poor:
        return ("red", "Critical")

    # Preferred: the real multi-day energy balance, once enough history exists.
    if have_bal:
        if balance < -0.05 * avg_cons_wh:   # spending more than we harvest, day over day
            days = (usable_wh / -balance) if usable_wh > 0 else None
            label = f"Drawing down ~{days:.0f}d" if days else "Drawing down"
            if good and charging_now:
                return ("yellow", label + " · recovering")
            return ("red" if (days is not None and days < 2) else "yellow", label)
        if balance >= 0.05 * avg_cons_wh and not poor:
            return ("green", "Self-sufficient")     # building surplus
        return ("green", "Sustaining")              # break-even / full but a thin forecast

    # Thin history: fall back to the instantaneous signal, honestly.
    if soc is not None and soc >= 95 and charging_now and not poor:
        return ("green", "Self-sufficient")         # full and being held there by the sun
    if charging_now and not poor:
        return ("green", "Sustaining")              # solar currently covering the load
    return ("gray", "Gathering data")               # discharging, can't yet judge the balance


def estimate_avg_load_w(conn, now, hours=LOAD_WINDOW_HOURS):
    """
    Trailing average house load (W) derived from energy balance, since the MPPT
    cannot measure load directly:  load = solar_harvested - change_in_stored.
    Solar harvested comes from H19 (monotonic lifetime yield counter, robust to
    the daily H20 reset); stored-energy change from voltage-based SOC at the
    window edges. Returns None when there isn't enough clean data to be useful.
    """
    since = (now - timedelta(hours=hours)).isoformat()
    pts = []  # (timestamp, voltage, h19_kwh)
    try:
        rows = conn.execute(
            "SELECT data FROM logs WHERE timestamp >= ? ORDER BY timestamp ASC", (since,)
        ).fetchall()
    except Exception:
        return None
    for (raw,) in rows:
        try:
            d = json.loads(raw)
            v = clean_int(d.get("V", 0)) / 1000
            if v < 11:  # skip noise / corrupt zero frames
                continue
            ts = ensure_utc(datetime.fromisoformat(d.get("timestamp")))
            pts.append((ts, v, clean_int(d.get("H19", 0)) / 100))
        except Exception:
            continue
    if len(pts) < 10:
        return None
    elapsed_h = (pts[-1][0] - pts[0][0]).total_seconds() / 3600
    if elapsed_h < 6:
        return None
    h19 = [p[2] for p in pts if p[2] > 0]
    if len(h19) < 2:
        return None
    solar_wh = (max(h19) - min(h19)) * 1000  # counter is monotonic, so max-min = harvested
    dstored_wh = BATTERY_CAPACITY_WH * (estimate_soc(pts[-1][1]) - estimate_soc(pts[0][1])) / 100
    load_w = (solar_wh - dstored_wh) / elapsed_h
    return load_w if 0.5 < load_w < 1000 else None


def build_voltage_series(parsed):
    """
    Extracts a voltage time series from parsed solar data for Chart.js.
    Returns (timestamps, values) where values below 11V are set to None (noise filter).
    """
    timestamps, values = [], []
    for ts, v, *_ in parsed:
        try:
            timestamps.append(fmt_mt(ts))
            values.append(round(v, 2) if v >= 11 else None)
        except Exception:
            continue
    return timestamps, values


def _sl_up(sl):
    """True when Starlink is actively connected (basis for the up/down streak)."""
    return bool(sl) and bool(sl.get("ok")) and sl.get("state") == "CONNECTED" and not sl.get("currently_obstructed")
