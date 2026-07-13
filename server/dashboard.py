"""
DeltaPi — dashboard context builder.

Turns the raw log rows + latest Pi status into the fully-computed template
context in two independent stages: build_context (DB-only — every pill
class/label, the readings table, the Chart.js data island) and build_external
(the Weather / Environment / solar-forecast fragments from the eight external
APIs, served later by /external so the page never waits on an upstream).
placeholder_context supplies the instant page shell. Pure compute — no request
handling, no HTML shell (that lives in templates/index.html).
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from dateutil.parser import parse as parse_date
from markupsafe import escape

from config import (MAX_DAYS, BATTERY_CAPACITY_WH, SOC_FLOOR, DB_DIR,
                    HOME_LAT, HOME_LON, HOME_DISH_ID, SOLAR_KWP, SOLAR_PR,
                    CS_MAP, ERR_MAP, WMO_CODES, AQI_BANDS, SEVERITY_RANK, FLOOD_LABELS)
from util import (server_log, humanize_minutes, clean_int, ensure_utc, fmt_mt,
                  fmt_clock, make_status_pill, fmt_runtime, _avg_complete_days, moon_phase)
from energy import (estimate_soc, soc_pill, sustainability_outlook,
                    estimate_avg_load_w, build_voltage_series, _sl_up)
from integrations import (get_weather, get_air_quality, get_nws_alerts, get_place,
                          get_wildfires, get_quake, get_aurora, get_river)
from db import get_disk_status, get_active_location

# One point per chart pixel is plenty: at one frame per ~15 s a 7-day window is
# ~40k rows, and shipping them all makes the chart data island multi-MB and the
# client-side Chart.js render crawl.
CHART_MAX_POINTS = 600


def _downsample(rows, max_points=CHART_MAX_POINTS):
    """Stride-sample a chronological list down to ~max_points for the charts,
    always keeping the newest point so the right edge stays current."""
    stride = max(1, len(rows) // max_points)
    if stride == 1:
        return rows
    sampled = rows[::stride]
    if sampled[-1] is not rows[-1]:
        sampled.append(rows[-1])
    return sampled


def build_context(conn, rows, days, now):
    """Compute the full template context for the dashboard from the queried log
    `rows` (newest-first), over the requested `days` window, as of `now` (UTC)."""
    parsed = []
    since = now - timedelta(days=days)

    # Logger status
    if rows:
        try:
            latest_entry = json.loads(rows[0][2])
            last_ts = datetime.fromisoformat(latest_entry.get("timestamp", rows[0][0]))
        except Exception:
            last_ts = datetime.fromisoformat(rows[0][0])
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - last_ts
        if delta.total_seconds() < 600:
            status_color, status_text = "green", "Receiving data"
        else:
            age_str = humanize_minutes(delta.total_seconds() / 60)
            status_color, status_text = "red", f"No data in {age_str}"
    else:
        status_color, status_text = "red", "No data available"

    # Pi status
    pi_status_row = None
    try:
        row = conn.execute(
            "SELECT ip, timestamp, uptime, cpu_temp, disk, memory, ssid, wifi_signal, fan_speed, pi_name, pi_os, pi_updates, controller, backup_count, backup_latest FROM pi_status ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        if row:
            pi_status_row = dict(zip(
                ["ip", "timestamp", "uptime", "cpu_temp", "disk", "memory", "ssid", "wifi_signal", "fan_speed", "pi_name", "pi_os", "pi_updates", "controller", "backup_count", "backup_latest"], row
            ))
    except Exception as e:
        server_log("GET", f"Failed to fetch Pi status: {e}", "warning")

    # Parse logs
    batt_series = []  # (ts, soc%, house_load_w) for frames carrying measured battery data
    sl_history = []   # (row_ts, up?) per frame carrying Starlink data, newest-first —
                      # captured here so the streak walk below needn't re-parse every row
    for row in rows:
        try:
            data = json.loads(row["data"])
            sl_frame = data.get("starlink")
            if sl_frame:
                sl_history.append((row["timestamp"], _sl_up(sl_frame)))
            ts = data.get("timestamp", row["timestamp"])
            ts_dt = ensure_utc(datetime.fromisoformat(ts))
            if ts_dt < since:
                continue
            v = clean_int(data.get("V", 0)) / 1000
            i = clean_int(data.get("I", 0)) / 1000
            ppv = clean_int(data.get("PPV", 0))
            vpv = clean_int(data.get("VPV", 0)) / 1000
            load = data.get("LOAD", "N/A")
            cs = CS_MAP.get(str(data.get("CS", "0")), f"Unknown ({data.get('CS')})")
            err = data.get("ERR", "0")
            h20 = clean_int(data.get("H20", 0)) / 100
            h21 = clean_int(data.get("H21", 0))
            parsed.append((ts, v, i, ppv, vpv, load, cs, err, h20, h21))
            bat = data.get("battery")
            if bat and bat.get("soc") is not None:
                # true house load = MPPT output (V*I) minus battery net power
                house = max(0, round(v * i - (bat.get("power") or 0), 1))
                temps = [t for p in (bat.get("per") or []) if p.get("ok")
                         for t in (p.get("temps_f") or []) if isinstance(t, (int, float))]
                batt_series.append((ts, bat["soc"], house, max(temps) if temps else None))
        except Exception as e:
            server_log("GET", f"Skipping row due to error: {e}", "warning")

    # Metrics
    parsed_chrono = []
    if not parsed:
        latest_voltage = "N/A"
        latest_vpv = 0
        vpv_message = "No data available"
        table_data = []
    else:
        voltages = [p[1] for p in parsed]
        currents = [p[2] for p in parsed]
        latest_voltage = f"{voltages[0]:.2f} V"
        parsed_chrono = list(reversed(parsed))
        table_data = parsed_chrono[-5:] if len(parsed_chrono) > 5 else parsed_chrono
        latest_vpv = parsed[0][4]
        # Panel VOLTAGE only indicates electrical status, NOT sunlight — panels hold
        # high voltage even in clouds/rain while producing almost no power. Actual
        # sun/production is the power-based "Solar power" pill, not this.
        vpv_message = (
            "Night" if latest_vpv < 5 else
            "Over-Voltage" if latest_vpv > 45 else
            "Nominal"
        )

    vpv_color = (
        "gray" if latest_vpv < 5 else
        "red" if latest_vpv > 45 else
        "gray"
    )

    existing_days = conn.execute("SELECT COUNT(DISTINCT DATE(timestamp)) FROM logs").fetchone()[0]

    # Daily energy (H20) aggregation
    daily_h20 = defaultdict(float)
    for row in reversed(parsed):
        try:
            ts = datetime.fromisoformat(row[0])
            day = ts.date().isoformat()
            daily_h20[day] = row[8]
        except Exception:
            continue

    h20_days = sorted(daily_h20.keys())
    h20_values = [round(daily_h20[day], 2) for day in h20_days]

    # Voltage / power charts — downsampled to ~CHART_MAX_POINTS. Aggregates
    # (daily kWh, daily consumption) still integrate over the full data.
    chart_rows = _downsample(parsed_chrono)
    voltage_timestamps, voltage_values = build_voltage_series(chart_rows)
    timestamps = [fmt_mt(p[0]) for p in chart_rows]
    powers = [p[3] for p in chart_rows]                          # PPV (panel input)
    charge_powers = [round(p[1] * p[2], 1) for p in chart_rows]  # MPPT output to battery (V*I)
    today_yield = parsed[0][8] if parsed else 0                  # H20, kWh produced today

    # Battery charts (measured) — chronological; only frames carrying battery data.
    batt_chrono = list(reversed(batt_series))
    batt_sampled = _downsample(batt_chrono)
    batt_times = [fmt_mt(b[0]) for b in batt_sampled]
    batt_soc_values = [b[1] for b in batt_sampled]
    batt_load_values = [b[2] for b in batt_sampled]
    batt_temp_values = [b[3] for b in batt_sampled]
    SOC_DANGER = 20  # red floor line: regularly draining below this shortens LiFePO4 life
    FREEZE_F = 32    # red line on the temp chart: LiFePO4 must not charge below freezing
    # Daily-energy chart: scale to the data instead of a fixed 1.6 ceiling
    h20_ymax = max([round(max(h20_values) * 1.25, 1), 0.5]) if h20_values else 1.6

    # Daily consumption (kWh) — trapezoidal-integrate measured house load per day,
    # skipping gaps > 30 min so feed downtime isn't counted as usage.
    daily_cons_wh = defaultdict(float)
    prev_t, prev_p = None, None
    for b in batt_chrono:
        try:
            t = datetime.fromisoformat(b[0])
        except Exception:
            continue
        if prev_t is not None:
            dt_h = (t - prev_t).total_seconds() / 3600
            if 0 < dt_h < 0.5:
                daily_cons_wh[t.date().isoformat()] += (b[2] + prev_p) / 2 * dt_h
        prev_t, prev_p = t, b[2]
    cons_days = sorted(daily_cons_wh.keys())
    cons_values = [round(daily_cons_wh[d] / 1000, 2) for d in cons_days]

    # Pi health pills
    try:
        last_checkin_dt = parse_date(pi_status_row['timestamp'])
        if last_checkin_dt.tzinfo is None:
            last_checkin_dt = last_checkin_dt.replace(tzinfo=timezone.utc)
        checkin_age_min = (datetime.now(timezone.utc) - last_checkin_dt).total_seconds() / 60
        checkin_class, checkin_label = make_status_pill(checkin_age_min, [
            (15, ("green", "recent")), (30, ("yellow", "moderate")), (float('inf'), ("red", "stale"))
        ])
        checkin_label = f"{humanize_minutes(checkin_age_min)} ago"
    except Exception:
        checkin_class, checkin_label = "gray", "Unknown"

    try:
        temp_c = float(pi_status_row['cpu_temp'].split("°C")[0].strip())
        temp_class, temp_label = make_status_pill(temp_c, [
            (50, ("green", "Cool")), (70, ("yellow", "Warm")), (float('inf'), ("red", "HOT"))
        ])
    except Exception:
        temp_class, temp_label = "gray", "Unknown"

    try:
        dBm = int(str(pi_status_row.get("wifi_signal", "-100")).split()[0])
        wifi_class, wifi_label = make_status_pill(dBm, [
            (-80, ("red", "Poor")), (-70, ("orange", "Weak")), (-65, ("green", "Fair")),
            (-50, ("green", "Good")), (float('inf'), ("green", "Strong"))
        ])
    except Exception:
        wifi_class, wifi_label = "gray", "Unknown"

    try:
        duty_value = int(pi_status_row.get("fan_speed", "0%").replace("%", "").strip())
        fan_class, fan_label = make_status_pill(duty_value, [
            (1, ("gray", "Off")), (50, ("green", "Low")), (80, ("green", "Moderate")), (float('inf'), ("yellow", "High"))
        ])
    except Exception:
        fan_class, fan_label = "gray", "Unknown"

    try:
        updates_value = int(pi_status_row.get("pi_updates", "0").split()[0])
        updates_class, updates_label = make_status_pill(updates_value, [
            (1, ("green", "Up to date")), (float('inf'), ("red", "Updates available"))
        ])
    except Exception:
        updates_class, updates_label = "gray", "Unknown"

    controller_val = pi_status_row.get("controller", "unknown") if pi_status_row else "unknown"
    if controller_val == "Connected":
        controller_class, controller_label = "green", "Connected"
    elif controller_val == "unknown":
        controller_class, controller_label = "gray", "Unknown"
    else:
        controller_class, controller_label = "red", "No Controller"

    # Charging state
    if parsed:
        latest_cs = parsed[0][6]
        latest_v = parsed[0][1]
        is_charging = latest_cs in ("Bulk", "Absorption")
        if 14.4 <= latest_v <= 14.6 and is_charging:
            latest_cs = "Fully Charging"
    else:
        latest_cs = "Unknown"
        latest_v = 0
        is_charging = False

    try:
        latest_voltage_val = float(latest_voltage.replace("V", "").strip())
        if latest_cs == "Fully Charging":
            latest_voltage_class, latest_voltage_label = "green", "Fully Charging"
        elif is_charging:
            latest_voltage_class, latest_voltage_label = "green", "Charging"
        else:
            latest_voltage_class, latest_voltage_label = make_status_pill(latest_voltage_val, [
                (12.8, ("red", "Discharge")), (13.0, ("orange", "Low")), (13.28, ("yellow", "Watch")),
                (13.5, ("green", "Good")), (14.0, ("green", "Full")), (float('inf'), ("green", "Charging"))
            ])
    except Exception:
        latest_voltage_class, latest_voltage_label = ("green", "Charging") if is_charging else ("gray", "Unknown")

    # ---- Measured battery data (from the BLE poller, embedded by the logger) ----
    # Prefer real coulomb-counted SOC / load when the feed is live; otherwise fall
    # back to the voltage-based estimate, clearly labeled. The feed's own ts/healthy
    # drive a health pill so a broken poller is visible rather than silently trusted.
    battery = None
    if rows:
        try:
            battery = json.loads(rows[0][2]).get("battery")
        except Exception:
            battery = None
    batt_age_s = None
    if battery and battery.get("ts"):
        try:
            batt_age_s = (datetime.now(timezone.utc) - ensure_utc(datetime.fromisoformat(battery["ts"]))).total_seconds()
        except Exception:
            batt_age_s = None

    def _age_str(sec):
        sec = int(sec)
        return f"{sec}s ago" if sec < 90 else f"{humanize_minutes(sec / 60)} ago"

    batt_live = bool(battery and batt_age_s is not None and batt_age_s < 180
                     and battery.get("healthy") and battery.get("soc") is not None)
    # Any measured reading (live OR stale). When present we always show the last
    # measured numbers (with a freshness pill), never the blunt energy-balance
    # estimate — that's only a last resort when there's no feed data at all.
    batt_present = bool(battery and battery.get("soc") is not None)
    if not battery or batt_age_s is None:
        batt_feed_class, batt_feed_label = "gray", "No feed"
    elif batt_age_s >= 600:
        batt_feed_class, batt_feed_label = "red", f"Offline ({_age_str(batt_age_s)})"
    elif batt_age_s >= 180:
        batt_feed_class, batt_feed_label = "yellow", f"Stale ({_age_str(batt_age_s)})"
    elif not battery.get("healthy"):
        down = [str(p.get("id") or p.get("label") or "?") for p in (battery.get("per") or []) if not p.get("ok")]
        detail = (", ".join(down) + " down") if down else f"{battery.get('ok')}/{battery.get('total')}"
        batt_feed_class, batt_feed_label = "yellow", f"Degraded — {detail}"
    else:
        batt_feed_class, batt_feed_label = "green", f"Live ({_age_str(batt_age_s)})"

    batt_per_str = ""
    if battery and battery.get("per"):
        parts = []
        for p in battery["per"]:
            if p.get("ok") and p.get("soc") is not None:
                name = p.get("id") or p.get("label")
                parts.append(f"{name}:{p['soc']}%" if name else f"{p['soc']}%")
        batt_per_str = " ".join(parts)
    batt_per_display = f"{batt_per_str} " if (batt_present and batt_per_str) else ""

    # SOC — measured whenever we have a reading (live or stale), else voltage estimate
    if batt_present:
        soc_percent = battery["soc"]
        soc_color, soc_label = soc_pill(soc_percent)
    elif parsed:
        voltage_float = voltages[0]
        soc_percent = estimate_soc(voltage_float)
        if is_charging:
            soc_color, soc_label = "yellow", "Estimated"
        else:
            soc_color, soc_label = soc_pill(soc_percent)
    else:
        soc_percent, soc_color, soc_label = 0, "gray", "Unknown"

    # Runtime. With a live feed it's all measured: true SOC, and true house load
    # computed as MPPT output power minus the battery's net power (valid whether
    # charging or discharging). Without the feed, fall back to the energy-balance
    # estimate (tagged "est").
    if batt_present:
        v = battery.get("voltage") or 0
        usable_wh = max(0, ((battery.get("remaining_ah") or 0)
                            - (battery.get("capacity_ah") or 0) * SOC_FLOOR / 100) * v)
        mppt_out_w = voltages[0] * currents[0] if parsed else 0
        house_load_w = max(0, mppt_out_w - (battery.get("power") or 0))  # true house load
        house_load_a = house_load_w / v if v else 0
        house_load_str = f"{house_load_w:.0f} W ({house_load_a:.1f} A)"
        runtime_str = fmt_runtime(house_load_w, usable_wh)
    else:
        avg_load_w = estimate_avg_load_w(conn, now) if parsed else None
        usable_wh = BATTERY_CAPACITY_WH * max(0, soc_percent - SOC_FLOOR) / 100 if parsed else 0
        house_load_str = f"~{avg_load_w:.0f} W est" if avg_load_w else "—"
        runtime_str = fmt_runtime(avg_load_w, usable_wh, " est") if avg_load_w else "N/A"

    # Current solar power (latest panel watts) for the Solar System panel
    solar_now = parsed[0][3] if parsed else 0
    charge_now_a = currents[0] if parsed else 0  # MPPT charge current into the battery (A)
    # Solar has no "bad" state, so color only conveys producing (green) vs off
    # (gray); the label carries the magnitude.
    solar_class, solar_label = make_status_pill(solar_now, [
        (5, ("gray", "Off")), (50, ("green", "Low")),
        (150, ("green", "Good")), (float('inf'), ("green", "Strong")),
    ])

    # Charge mode (CS) — green while charging, red on fault, gray when idle/off
    if parsed:
        mode_label = parsed[0][6]
        mode_class = ("green" if mode_label in ("Bulk", "Absorption", "Float")
                      else "red" if mode_label == "Fault" else "gray")
    else:
        mode_class, mode_label = "gray", "Unknown"

    # Controller health from the ERR code — green "OK", red with the reason
    if parsed:
        err_code = str(parsed[0][7]).replace("\x00", "").strip() or "0"
        if err_code == "0":
            ctrl_class, ctrl_label = "green", "OK"
        else:
            ctrl_class, ctrl_label = "red", ERR_MAP.get(err_code, f"Error {err_code}")
    else:
        ctrl_class, ctrl_label = "gray", "Unknown"

    # Battery temperature, cell balance, time-to-full (from the measured feed)
    batt_temps, batt_deltas = [], []
    for p in (battery.get("per") if battery else None) or []:
        if p.get("ok"):
            batt_temps += [t for t in (p.get("temps_f") or []) if isinstance(t, (int, float))]
            if p.get("cell_delta") is not None:
                batt_deltas.append(p["cell_delta"])

    if batt_present and batt_temps:
        tmin, tmax = min(batt_temps), max(batt_temps)
        batt_temp_str = f"{tmax:.0f}°F"
        if tmin < 32:      # LiFePO4 must not charge below freezing
            btemp_class, btemp_label = "red", "Too cold to charge"
        elif tmin < 40:
            btemp_class, btemp_label = "yellow", "Cold"
        elif tmax > 113:   # ~45°C
            btemp_class, btemp_label = "red", "Hot"
        else:
            btemp_class, btemp_label = "green", "OK"
    else:
        batt_temp_str, btemp_class, btemp_label = "—", "gray", "n/a"

    if batt_present and batt_deltas:
        dmax = max(batt_deltas)
        pack_v = battery.get("voltage") if battery else None
        # Near full charge the LiFePO4 curve goes near-vertical, so cells fan out
        # by ~0.1V while the BMS top-balances — that's expected, not a fault. Don't
        # alarm on the spread at top-of-charge (full SOC and on the charger); a
        # genuinely weak cell still surfaces mid-discharge, and a truly extreme
        # spread alarms even here.
        top_of_charge = (soc_percent is not None and soc_percent >= 99
                         and (is_charging or (pack_v is not None and pack_v >= 13.9)))
        if dmax >= 0.20:
            cell_class, cell_label = "red", f"{dmax:.2f}V spread"
        elif top_of_charge:
            cell_class, cell_label = ("green", "Balancing") if dmax >= 0.05 else ("green", "Balanced")
        elif dmax >= 0.10:
            cell_class, cell_label = "red", f"{dmax:.2f}V spread"
        elif dmax >= 0.05:
            cell_class, cell_label = "yellow", f"{dmax:.2f}V spread"
        else:
            cell_class, cell_label = "green", "Balanced"
    else:
        cell_class, cell_label = "gray", "—"

    ttf_str = "—"
    if batt_present and battery:
        cur = battery.get("current") or 0   # bank net current, + = charging
        rem = battery.get("remaining_ah") or 0
        cap = battery.get("capacity_ah") or 0
        if soc_percent and soc_percent >= 99:
            ttf_str = "Full"
        elif cur <= 0.2:
            ttf_str = "—"                       # not meaningfully charging (idle / discharging)
        elif soc_percent and soc_percent >= 90:
            ttf_str = "Topping off"             # absorption/float taper -> linear estimate is unreliable
        elif cap > rem:
            ttf_str = humanize_minutes((cap - rem) / cur * 60)  # bulk stage: ~constant current, linear is fair

    # ---- Starlink connectivity (dish status via starlink_poll, merged by logger) ----
    starlink = None
    if rows:
        try:
            starlink = json.loads(rows[0][2]).get("starlink")
        except Exception:
            starlink = None
    sl_age = None
    if starlink and starlink.get("timestamp"):
        try:
            sl_age = (datetime.now(timezone.utc) - ensure_utc(datetime.fromisoformat(starlink["timestamp"]))).total_seconds()
        except Exception:
            sl_age = None

    if not starlink or sl_age is None:
        sl_status_class, sl_status_label = "gray", "No data"
    elif sl_age >= 600 or not starlink.get("ok"):
        sl_status_class, sl_status_label = "red", "Offline"
    elif starlink.get("currently_obstructed"):
        sl_status_class, sl_status_label = "yellow", "Obstructed"
    elif starlink.get("state") == "CONNECTED":
        sl_status_class, sl_status_label = "green", "Online"
    else:
        sl_status_class, sl_status_label = "yellow", str(starlink.get("state") or "?").title()

    # How long Starlink has held its current up/down state: walk the per-frame
    # up/down flags captured in the parse loop (newest-first) until the state flips.
    sl_streak_html = ""
    if starlink:
        cur_up = _sl_up(starlink)
        streak_start, hit_edge = None, True
        for r_ts, r_up in sl_history:
            if r_up == cur_up:
                streak_start = r_ts          # extend the streak back to this frame
            else:
                hit_edge = False             # found the transition
                break
        if streak_start:
            try:
                mins = (now - ensure_utc(datetime.fromisoformat(streak_start))).total_seconds() / 60
                pre = "≥" if hit_edge else ""          # ≥ means the streak predates our window
                lbl = "Online for" if cur_up else "Offline for"
                sl_streak_html = f'<div class="metric"><span class="metric-label">{lbl}</span><span class="metric-value">{pre}{humanize_minutes(mins)}</span></div>'
            except Exception:
                sl_streak_html = ""

    sl_obs = starlink.get("obstruction_pct") if starlink else None
    if sl_obs is None:
        sl_obs_class, sl_obs_label = "gray", "—"
    elif (starlink.get("currently_obstructed") if starlink else False) or sl_obs >= 5:
        sl_obs_class, sl_obs_label = "red", "Blocked"
    elif sl_obs >= 1:
        sl_obs_class, sl_obs_label = "yellow", "Some"
    else:
        sl_obs_class, sl_obs_label = "green", "Clear"
    sl_obs_str = f"{sl_obs}%" if sl_obs is not None else "—"

    sl_alerts = (starlink.get("alerts") if starlink else None) or []
    if not starlink:
        sl_alert_class, sl_alert_label = "gray", "—"
    elif sl_alerts:
        sl_alert_class, sl_alert_label = "red", escape(", ".join(sl_alerts))
    else:
        sl_alert_class, sl_alert_label = "green", "None"

    sl_down = starlink.get("down_mbps") if starlink else None
    sl_up = starlink.get("up_mbps") if starlink else None
    sl_latency = starlink.get("latency_ms") if starlink else None
    sl_speed_str = f"{sl_down:.1f}↓ / {sl_up:.1f}↑ Mbps" if sl_down is not None else "—"
    sl_latency_str = f"{sl_latency:.0f} ms" if sl_latency is not None else "—"

    # ---- Dish model: auto-detect Mini vs Home from the reported hardware version.
    # The Mini reports "mini…" (e.g. mini1_panda_proto1); the round dish reports
    # rev*/…_pre_production — neither contains "mini". (Confirmed against both.) ----
    dish_hw = (starlink or {}).get("hardware_version")
    if dish_hw:
        dish_class, dish_label = "gray", ("Starlink Mini" if "mini" in dish_hw.lower() else "Starlink Home")
    else:
        dish_class, dish_label = "gray", "—"

    # ---- Weather location: manual pin (header dropdown) -> dish GPS (Mini) ->
    # home dish id -> home coords; else unknown. The manual pin wins so a dish
    # that won't share GPS (e.g. the Mini) can still get local weather. ----
    dish_id = (starlink or {}).get("id")
    dlat, dlon = (starlink or {}).get("lat"), (starlink or {}).get("lon")
    manual_loc = get_active_location(conn)               # None = Auto (follow GPS)
    if manual_loc:
        wx_lat, wx_lon = manual_loc["lat"], manual_loc["lon"]   # user-pinned spot
    elif dlat is not None:
        wx_lat, wx_lon = dlat, dlon                      # dish sharing GPS (Mini on the road)
    elif HOME_DISH_ID and dish_id and dish_id != HOME_DISH_ID:
        wx_lat = wx_lon = None                           # positively roaming on a no-GPS dish
    else:
        wx_lat, wx_lon = HOME_LAT, HOME_LON              # home / can't tell -> home coords

    # The eight external lookups (weather, alerts, AQI, …) are deferred to the
    # /external endpoint so neither the page shell nor the local data ever waits
    # on a third-party API. Everything that stage needs from this parse is
    # bundled into ext_inputs at the bottom of the context.

    # Sustainability Outlook (Solar panel): fuse measured daily harvest vs
    # consumption, the current charge state, and the solar forecast into one
    # forward-looking state (Self-sufficient / Sustaining / Drawing down / Critical).
    ah = _avg_complete_days(h20_values)    # avg recent daily harvest (kWh)
    ac = _avg_complete_days(cons_values)   # avg recent daily consumption (kWh)
    net_w = battery.get("power") if batt_present else None   # + = charging
    charging_now = bool(is_charging) or (net_w is not None and net_w >= 0)

    # Power source: infer shore/generator vs off-grid from the energy balance.
    # The MPPT only sees solar; the battery BMS sees net current from ALL sources.
    # If the pack is charging faster than solar can supply, an external AC charger
    # (shore power or a generator) is doing it. This detects active charging — not
    # mere connection — and can't tell shore from a generator.
    ps_class, ps_label = "gray", "—"
    if batt_present and net_w is not None:
        solar_to_batt_w = (voltages[0] * currents[0]) if parsed else 0   # MPPT output to battery (W)
        if net_w - solar_to_batt_w > max(25.0, 0.15 * solar_to_batt_w):
            ps_class, ps_label = "gray", "Shore / Generator"
        else:
            ps_class, ps_label = "green", "Solar / Battery"

    # Preliminary outlook with an "unknown" forecast tier; /external recomputes
    # it once the weather forecast is in and dashboard.js swaps the pill.
    outlook_class, outlook_label = sustainability_outlook(
        batt_present,
        soc_percent if batt_present else None,
        charging_now,
        usable_wh,
        ah * 1000 if ah is not None else None,
        ac * 1000 if ac is not None else None,
        "unknown",
    )

    ext_inputs = {
        "wx_lat": wx_lat, "wx_lon": wx_lon, "ah": ah, "ac": ac,
        "loc_name": manual_loc["name"] if manual_loc else None,   # pinned label, if any
        "batt_present": batt_present,
        "soc_percent": soc_percent if batt_present else None,
        "charging_now": charging_now, "usable_wh": usable_wh,
    }

    # Disk status
    data_percent, data_class, data_label = get_disk_status(DB_DIR)

    # Readings table (chronological, oldest-first for display) — load/cs come from
    # VE.Direct payloads (user-controlled via /log), so the template auto-escapes them.
    table_rows = []
    for ts, v, i, ppv, vpv, load, cs, err, h20, h21 in reversed(table_data):
        table_rows.append({
            "time": fmt_mt(ts), "v": v, "i": i, "power": round(v * i, 2),
            "vpv": vpv, "load": load, "cs": cs, "h20": h20, "h21": h21,
        })

    # Pi-reported fields (auto-escaped by the template). Only used when pi_status_row exists.
    if pi_status_row:
        pi_name = (pi_status_row.get('pi_name') or '?').upper()
        pi_os_val = pi_status_row.get('pi_os') or '?'
        pi_uptime = pi_status_row.get('uptime') or '?'
        pi_cpu_temp = pi_status_row.get('cpu_temp') or '?'
        pi_fan_speed = pi_status_row.get('fan_speed') or '?'
        pi_updates_val = pi_status_row.get('pi_updates') or '?'
        pi_memory = pi_status_row.get('memory') or '?'
        pi_disk = pi_status_row.get('disk') or '?'
        pi_ssid = pi_status_row.get('ssid') or '?'
        pi_wifi_signal = pi_status_row.get('wifi_signal') or '?'
    else:
        pi_name = pi_os_val = pi_uptime = pi_cpu_temp = pi_fan_speed = None
        pi_updates_val = pi_memory = pi_disk = pi_ssid = pi_wifi_signal = None

    # Backup health: the puller runs daily, so a latest backup < ~26 h old is
    # healthy; older (or none) flips the pill so a silently-stopped puller shows.
    _bc = pi_status_row.get("backup_count") if pi_status_row else None
    backup_count_disp = _bc if (_bc and _bc != "unknown") else "—"
    _bl = pi_status_row.get("backup_latest") if pi_status_row else None
    try:
        _bage = (datetime.now(timezone.utc) - ensure_utc(datetime.fromisoformat(_bl))).total_seconds() / 60
        backup_age_label = f"{humanize_minutes(_bage)} ago"
        backup_class, backup_pill_label = make_status_pill(_bage, [
            (26 * 60, ("green", "fresh")), (50 * 60, ("yellow", "late")), (float('inf'), ("red", "stale"))
        ])
    except Exception:
        backup_age_label, backup_class, backup_pill_label = "never", "gray", "none"

    # ---- Pi health history for the Pi charts, bounded by the 7-day pi_status
    # retention. Values are stored as display strings, so pull the leading number
    # from each: °C for temp, MB-used for memory, % for fan, 1-min load average
    # (cpu_load is blank on rows posted before the Pi logger started sending it). ----
    def _first_num(s):
        m = re.search(r'-?\d+(?:\.\d+)?', s) if s else None
        return float(m.group()) if m else None

    pi_hist = []
    try:
        pi_hist = conn.execute(
            "SELECT timestamp, cpu_temp, memory, fan_speed, cpu_load, disk FROM pi_status "
            "WHERE timestamp >= ? ORDER BY timestamp ASC", (since.isoformat(),)
        ).fetchall()
    except Exception as e:
        server_log("GET", f"Pi history query failed: {e}", "warning")
    stride = max(1, len(pi_hist) // 600)   # downsample (pi_status posts every ~30 s)
    pi_sampled = pi_hist[::stride]
    pi_times = [fmt_mt(r[0]) for r in pi_sampled]
    pi_temp_vals = [_first_num(r[1]) for r in pi_sampled]   # °C
    pi_mem_vals = [_first_num(r[2]) for r in pi_sampled]    # MB used
    pi_fan_vals = [_first_num(r[3]) for r in pi_sampled]    # % duty
    pi_load_vals = [_first_num(r[4]) for r in pi_sampled]   # 1-min load average
    pi_disk_vals = [_first_num(r[5]) for r in pi_sampled]   # GB used (leading number of "12.3G/32G (39%)")

    # Y-axis ceilings so usage reads against the machine's actual capacity:
    # memory/disk totals parsed from the latest status strings ("used/total (pct%)"
    # and "usedG/totalG (pct%)"); CPU load tops out at 4 (every recent Pi is
    # quad-core, so 4.0 = all cores busy), stretched if load ever exceeds it.
    # None (unparseable / no Pi data) lets Chart.js fall back to auto-scaling.
    def _total_of(s):
        m = re.search(r'/\s*(\d+(?:\.\d+)?)', s) if s else None
        return float(m.group(1)) if m else None

    pi_mem_max = _total_of(pi_status_row.get("memory")) if pi_status_row else None
    pi_disk_max = _total_of(pi_status_row.get("disk")) if pi_status_row else None
    pi_load_max = max([4.0] + [v for v in pi_load_vals if v is not None])

    chart_payload = {
        "timestamps": timestamps, "powers": powers, "charge_powers": charge_powers,
        "voltage_timestamps": voltage_timestamps, "voltage_values": voltage_values,
        "h20_days": h20_days, "h20_values": h20_values, "h20_ymax": h20_ymax,
        "batt_times": batt_times, "batt_soc_values": batt_soc_values,
        "batt_load_values": batt_load_values, "batt_temp_values": batt_temp_values,
        "cons_days": cons_days, "cons_values": cons_values,
        "SOC_DANGER": SOC_DANGER, "FREEZE_F": FREEZE_F,
        "pi_times": pi_times, "pi_temp_vals": pi_temp_vals, "pi_mem_vals": pi_mem_vals,
        "pi_fan_vals": pi_fan_vals, "pi_load_vals": pi_load_vals, "pi_disk_vals": pi_disk_vals,
        "pi_mem_max": pi_mem_max, "pi_disk_max": pi_disk_max, "pi_load_max": pi_load_max,
    }

    return {
        # header
        "days": days, "MAX_DAYS": MAX_DAYS, "booting": False,
        # battery panel
        "soc_percent": soc_percent, "soc_color": soc_color, "soc_label": soc_label,
        "latest_voltage": latest_voltage, "latest_voltage_class": latest_voltage_class,
        "latest_voltage_label": latest_voltage_label,
        "batt_per_display": batt_per_display, "batt_feed_class": batt_feed_class,
        "batt_feed_label": batt_feed_label, "ps_class": ps_class, "ps_label": ps_label,
        "batt_temp_str": batt_temp_str, "btemp_class": btemp_class, "btemp_label": btemp_label,
        "cell_class": cell_class, "cell_label": cell_label,
        "house_load_str": house_load_str, "runtime_str": runtime_str, "ttf_str": ttf_str,
        # solar panel
        "status_color": status_color, "status_text": status_text,
        "ctrl_class": ctrl_class, "ctrl_label": ctrl_label,
        "mode_class": mode_class, "mode_label": mode_label,
        "solar_now": solar_now, "charge_now_a_str": f"{charge_now_a:.1f}",
        "solar_class": solar_class, "solar_label": solar_label,
        "today_yield_str": f"{today_yield:.2f}",
        "latest_vpv_str": f"{latest_vpv:.2f}", "vpv_color": vpv_color, "vpv_message": vpv_message,
        "outlook_class": outlook_class, "outlook_label": outlook_label,
        # starlink panel
        "sl_status_class": sl_status_class, "sl_status_label": sl_status_label,
        "dish_class": dish_class, "dish_label": dish_label, "sl_streak_html": sl_streak_html,
        "sl_obs_str": sl_obs_str, "sl_obs_class": sl_obs_class, "sl_obs_label": sl_obs_label,
        "sl_alert_class": sl_alert_class, "sl_alert_label": sl_alert_label,
        "sl_speed_str": sl_speed_str, "sl_latency_str": sl_latency_str,
        # pi health panel
        "pi_status_row": pi_status_row,
        "pi_name": pi_name, "pi_os_val": pi_os_val, "pi_uptime": pi_uptime,
        "pi_cpu_temp": pi_cpu_temp, "pi_fan_speed": pi_fan_speed, "pi_updates_val": pi_updates_val,
        "pi_memory": pi_memory, "pi_disk": pi_disk, "pi_ssid": pi_ssid, "pi_wifi_signal": pi_wifi_signal,
        "backup_count_disp": backup_count_disp, "backup_age_label": backup_age_label,
        "backup_class": backup_class, "backup_pill_label": backup_pill_label,
        "controller_class": controller_class, "controller_label": controller_label,
        "checkin_class": checkin_class, "checkin_label": checkin_label,
        "temp_class": temp_class, "temp_label": temp_label,
        "fan_class": fan_class, "fan_label": fan_label,
        "updates_class": updates_class, "updates_label": updates_label,
        "wifi_class": wifi_class, "wifi_label": wifi_label,
        "data_percent": data_percent, "data_class": data_class, "data_label": data_label,
        "existing_days": existing_days,
        # table + charts
        "table_rows": table_rows, "chart_payload": chart_payload,
        # inputs for the deferred /external stage (popped by the route, never rendered)
        "ext_inputs": ext_inputs,
    }


def build_external(inp):
    """Fetch the eight external providers and build the deferred page fragments:
    the Weather / Environment panel bodies, the Solar-forecast row, and the final
    Sustainability Outlook (whose tier depends on the weather forecast). `inp` is
    the ext_inputs dict produced by build_context; the /external route serves the
    result after the shell and local panels have already been delivered."""
    wx_lat, wx_lon = inp["wx_lat"], inp["wx_lon"]
    ah, ac = inp["ah"], inp["ac"]

    # All eight external lookups are independent (lat, lon) fetches with their own
    # caches and timeouts, so run them concurrently: a cold render costs roughly
    # the single slowest call instead of the serial sum, and one hung upstream
    # costs its timeout rather than stalling the whole chain. Each provider
    # swallows its own failures (returns None/cached), so result() never raises.
    with ThreadPoolExecutor(max_workers=8) as pool:
        ext = {name: pool.submit(fn, wx_lat, wx_lon) for name, fn in (
            ("weather", get_weather), ("place", get_place), ("alerts", get_nws_alerts),
            ("fires", get_wildfires), ("aqi", get_air_quality), ("quake", get_quake),
            ("river", get_river), ("aurora", get_aurora),
        )}
        ext = {name: f.result() for name, f in ext.items()}

    wx = ext["weather"]
    forecast_tier = "unknown"   # solar forecast tier, feeds the Sustainability Outlook
    if wx:
        cur = wx.get("current") or {}
        daily = wx.get("daily") or {}
        wx_cond = WMO_CODES.get(cur.get("weather_code"), "—")
        wx_temp, wx_cloud = cur.get("temperature_2m"), cur.get("cloud_cover")
        codes = daily.get("weather_code") or []
        rad = daily.get("shortwave_radiation_sum") or []
        lows = [t for t in (daily.get("temperature_2m_min") or []) if t is not None]
        tomo_cond = WMO_CODES.get(codes[1], "—") if len(codes) > 1 else "—"
        tomo_rad = rad[1] if len(rad) > 1 else None
        if tomo_rad is None:
            chg_class, chg_label, forecast_tier = "gray", "—", "unknown"
        elif tomo_rad >= 18:
            chg_class, chg_label, forecast_tier = "green", "Good sun", "good"
        elif tomo_rad >= 8:
            chg_class, chg_label, forecast_tier = "yellow", "Fair", "fair"
        else:
            chg_class, chg_label, forecast_tier = "red", "Poor", "poor"
        temp_str = f"{wx_temp:.0f}°F" if wx_temp is not None else "—"
        cloud_str = f"{wx_cloud}%" if wx_cloud is not None else "—"
        weather_html = (
            f'<div class="metric"><span class="metric-label">Now</span><span class="metric-value">{escape(wx_cond)}, {temp_str}</span></div>'
            f'<div class="metric"><span class="metric-label">Cloud cover</span><span class="metric-value">{cloud_str}</span></div>'
        )
        # Current atmospherics (wind, humidity, storm nowcast).
        wind, gust = cur.get("wind_speed_10m"), cur.get("wind_gusts_10m")
        if wind is not None:
            if gust is not None and gust >= 45:
                w_pill = ' <span class="pill red">High wind</span>'
            elif gust is not None and gust >= 30:
                w_pill = ' <span class="pill yellow">Breezy</span>'
            else:
                w_pill = ""
            g_str = f" · gusts {gust:.0f}" if gust is not None else ""
            weather_html += f'<div class="metric"><span class="metric-label">Wind</span><span class="metric-value">{wind:.0f} mph{g_str}{w_pill}</span></div>'
        rh, dew, t_now = cur.get("relative_humidity_2m"), cur.get("dew_point_2m"), cur.get("temperature_2m")
        if rh is not None:
            d_str = f" · dew {dew:.0f}°F" if dew is not None else ""
            cond = ' <span class="pill yellow">Condensation likely</span>' if (
                dew is not None and t_now is not None and (t_now - dew) < 5) else ""
            weather_html += f'<div class="metric"><span class="metric-label">Humidity</span><span class="metric-value">{rh:.0f}%{d_str}{cond}</span></div>'
        m15 = wx.get("minutely_15") or {}
        mt, mp = m15.get("time") or [], m15.get("precipitation") or []
        off = wx.get("utc_offset_seconds") or 0
        local_now_str = (datetime.now(timezone.utc).replace(tzinfo=None)
                         + timedelta(seconds=off)).strftime("%Y-%m-%dT%H:%M")
        soon = sum((mp[i] or 0) for i in [j for j, t in enumerate(mt) if t >= local_now_str][:4] if i < len(mp))
        if cur.get("weather_code") in (95, 96, 99):
            s_cls, s_lbl = "red", "Thunderstorm now"
        elif cur.get("precipitation"):
            s_cls, s_lbl = "yellow", "Raining now"
        elif soon > 0.1:
            s_cls, s_lbl = "yellow", "Rain within the hour"
        elif (cur.get("cape") or 0) >= 1500:
            s_cls, s_lbl = "yellow", "Thunderstorm potential"
        else:
            s_cls, s_lbl = "green", "Calm"
        weather_html += f'<div class="metric"><span class="metric-label">Storm nowcast</span><span class="metric-value"><span class="pill {s_cls}">{s_lbl}</span></span></div>'
        # Forecast + overnight.
        weather_html += f'<div class="metric"><span class="metric-label">Tomorrow</span><span class="metric-value">{escape(tomo_cond)}</span></div>'
        weather_html += f'<div class="metric"><span class="metric-label">Charging outlook</span><span class="metric-value"><span class="pill {chg_class}">{chg_label}</span></span></div>'
        low0 = (daily.get("temperature_2m_min") or [None])[0]
        if low0 is not None:
            weather_html += f'<div class="metric"><span class="metric-label">Tonight\'s low</span><span class="metric-value">{low0:.0f}°F</span></div>'
        if any(t < 32 for t in lows):
            weather_html += '<div class="metric"><span class="metric-label">Freeze</span><span class="metric-value"><span class="pill red">Freeze risk — heater may run</span></span></div>'
    else:
        weather_html = '<div class="metric"><span class="metric-label">Status</span><span class="metric-value"><span class="pill gray">No location</span></span></div>'

    # Location as the Weather panel's first metric: the pinned name if the user
    # chose one, else the reverse-geocoded point (dish GPS / home), else raw coords.
    place = ext["place"]
    loc_name = inp.get("loc_name")
    if loc_name:
        loc_val = escape(loc_name)
    elif place:
        loc_val = escape(place)
    elif wx_lat is not None:
        loc_val = f"{wx_lat:.2f}, {wx_lon:.2f}"
    else:
        loc_val = None
    if loc_val:
        weather_html = (f'<div class="metric"><span class="metric-label">Location</span>'
                        f'<span class="metric-value">{loc_val}</span></div>' + weather_html)

    # Environment panel: severe-weather alerts (NWS), air quality (Open-Meteo),
    # and today's solar window (from the forecast's sunrise/sunset).
    environment_html = ""

    alerts = ext["alerts"]
    if alerts is None:
        environment_html += '<div class="metric"><span class="metric-label">Alert</span><span class="metric-value"><span class="pill gray">—</span></span></div>'
    elif not alerts:
        environment_html += '<div class="metric"><span class="metric-label">Alert</span><span class="metric-value"><span class="pill green">None</span></span></div>'
    else:
        top = max(alerts, key=lambda a: SEVERITY_RANK.get(a.get("severity"), 0))
        extra = f" (+{len(alerts) - 1})" if len(alerts) > 1 else ""
        a_cls = "red" if SEVERITY_RANK.get(top.get("severity"), 0) >= 3 else "yellow"
        environment_html += f'<div class="metric"><span class="metric-label">Alert</span><span class="metric-value"><span class="pill {a_cls}">{escape((top.get("event") or "Alert") + extra)}</span></span></div>'

    # Wildfire proximity (NASA FIRMS) — actual fire detections, vs the AQI/PM2.5 smoke proxy.
    fires = ext["fires"]
    if fires is None:
        environment_html += '<div class="metric"><span class="metric-label">Wildfire</span><span class="metric-value"><span class="pill gray">—</span></span></div>'
    elif not fires:
        environment_html += '<div class="metric"><span class="metric-label">Wildfire</span><span class="metric-value"><span class="pill green">None within 60 mi</span></span></div>'
    else:
        nm, brg, cnt = fires["nearest_mi"], fires["bearing"], fires["count"]
        f_cls, f_lbl = ("red", "Close") if nm <= 10 else ("yellow", "Nearby") if nm <= 50 else ("green", "Distant")
        more = f" · {cnt} within 60 mi" if cnt > 1 else ""
        environment_html += f'<div class="metric"><span class="metric-label">Wildfire</span><span class="metric-value">{nm:.0f} mi {brg}{more} <span class="pill {f_cls}">{f_lbl}</span></span></div>'

    aq = (ext["aqi"] or {}).get("current") or {}
    aqi, pm = aq.get("us_aqi"), aq.get("pm2_5")
    if aqi is None:
        environment_html += '<div class="metric"><span class="metric-label">Air quality</span><span class="metric-value"><span class="pill gray">—</span></span></div>'
    else:
        aq_cls, aq_lbl = next((c, l) for mx, c, l in AQI_BANDS if aqi <= mx)
        pm_str = f" · PM2.5 {pm:.0f}" if pm is not None else ""
        environment_html += f'<div class="metric"><span class="metric-label">Air quality</span><span class="metric-value">AQI {aqi:.0f}{pm_str} <span class="pill {aq_cls}">{aq_lbl}</span></span></div>'

    # Recent earthquake nearby (USGS) — most significant within ~250 mi over 24 h.
    quake = ext["quake"]
    if quake is None:
        environment_html += '<div class="metric"><span class="metric-label">Earthquake</span><span class="metric-value"><span class="pill gray">—</span></span></div>'
    elif not quake:
        environment_html += '<div class="metric"><span class="metric-label">Earthquake</span><span class="metric-value"><span class="pill green">None nearby</span></span></div>'
    else:
        mag, qd, qb, mins = quake["mag"], quake["dist_mi"], quake["bearing"], quake["mins_ago"]
        q_cls = "red" if (mag >= 5 and qd <= 100) else "yellow" if mag >= 4 else "gray"
        when = ""
        if mins is not None:
            when = f" · {mins / 60:.0f}h ago" if mins >= 90 else f" · {mins:.0f}m ago"
        q_pill = f' <span class="pill {q_cls}">{"Strong" if q_cls == "red" else "Notable"}</span>' if q_cls != "gray" else ""
        environment_html += f'<div class="metric"><span class="metric-label">Earthquake</span><span class="metric-value">M{mag:.1f} · {qd:.0f} mi {qb}{when}{q_pill}</span></div>'

    # (Wind, Humidity, Storm nowcast, Tonight's low now live in the Weather panel.)

    # Nearest river gauge + NWS flood status (USGS gage height + NOAA NWPS).
    # Omitted when no gauge is nearby (common away from rivers).
    river = ext["river"]
    if river:
        rname = (river.get("name") or "River").split(",")[0]
        if len(rname) > 32:
            rname = rname[:31] + "…"
        flood, stage = river.get("flood"), river.get("stage")
        st_str = f"{stage:.1f} ft" if stage is not None else "—"
        r_cls = ("red" if flood in ("major", "moderate")
                 else "yellow" if flood in ("minor", "action")
                 else "green" if flood == "no_flooding" else "gray")
        environment_html += f'<div class="metric"><span class="metric-label">River</span><span class="metric-value">{escape(rname)} · {st_str} <span class="pill {r_cls}">{FLOOD_LABELS.get(flood, "—")}</span></span></div>'

    sun_is_down = None              # set by the solar-window block below; feeds Stargazing
    sun_frac_left = None            # fraction of today's daylight remaining; feeds Solar forecast
    window_str = "—"
    if wx:
        daily = wx.get("daily") or {}
        sr = (daily.get("sunrise") or [None])[0]
        ss = (daily.get("sunset") or [None])[0]
        off = wx.get("utc_offset_seconds") or 0
        if sr and ss:
            try:
                sr_dt, ss_dt = datetime.fromisoformat(sr), datetime.fromisoformat(ss)
                local_now = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=off)
                if local_now < sr_dt:
                    tail, sun_is_down, sun_frac_left = " · before sunrise", True, 1.0
                elif local_now > ss_dt:
                    tail, sun_is_down, sun_frac_left = " · sun down", True, 0.0
                else:
                    sun_frac_left = (ss_dt - local_now).total_seconds() / max(1.0, (ss_dt - sr_dt).total_seconds())
                    tail, sun_is_down = f" · {(ss_dt - local_now).total_seconds() / 3600:.1f} h left", False
                window_str = f"{fmt_clock(sr_dt)}–{fmt_clock(ss_dt)}{tail}"
                # Fold TODAY's remaining sun into the Sustainability Outlook tier (the
                # sun can keep us up if there's good sun left today OR a good tomorrow).
                # The Weather panel's "Charging outlook" stays tomorrow-only.
                rad_list = daily.get("shortwave_radiation_sum") or []
                today_rad = rad_list[0] if rad_list else None
                tomo_rad2 = rad_list[1] if len(rad_list) > 1 else None
                if today_rad is not None or tomo_rad2 is not None:
                    if local_now <= sr_dt:
                        frac = 1.0
                    elif local_now >= ss_dt:
                        frac = 0.0
                    else:
                        frac = (ss_dt - local_now).total_seconds() / max(1.0, (ss_dt - sr_dt).total_seconds())
                    forward = max((today_rad or 0) * frac, tomo_rad2 or 0)
                    forecast_tier = "good" if forward >= 18 else "fair" if forward >= 8 else "poor"
            except Exception:
                window_str = "—"
    environment_html += f'<div class="metric"><span class="metric-label">Solar window</span><span class="metric-value">{window_str}</span></div>'
    if wx and wx.get("elevation") is not None:
        environment_html += f'<div class="metric"><span class="metric-label">Elevation</span><span class="metric-value">{wx["elevation"] * 3.28084:,.0f} ft</span></div>'
    mp_name, mp_illum = moon_phase(datetime.now(timezone.utc))
    environment_html += f'<div class="metric"><span class="metric-label">Moon</span><span class="metric-value">{mp_name} · {mp_illum}%</span></div>'

    # Aurora — overhead probability + planetary Kp (NOAA SWPC). Green = likely visible.
    aurora = ext["aurora"]
    if aurora and (aurora.get("prob") is not None or aurora.get("kp") is not None):
        prob, kp = aurora.get("prob"), aurora.get("kp")
        parts = []
        if kp is not None:
            parts.append(f"Kp {kp:.0f}")
        if prob is not None:
            parts.append(f"{prob:.0f}% overhead")
        if prob is not None and prob >= 30:
            au_cls, au_lbl = "green", "Likely"
        elif prob is not None and prob >= 10:
            au_cls, au_lbl = "yellow", "Possible"
        elif kp is not None and kp >= 5:
            au_cls, au_lbl = "yellow", "Storm"
        else:
            au_cls, au_lbl = "gray", "Quiet"
        environment_html += f'<div class="metric"><span class="metric-label">Aurora</span><span class="metric-value">{" · ".join(parts)} <span class="pill {au_cls}">{au_lbl}</span></span></div>'

    # Stargazing score — clear skies × low moon, only meaningful after dark.
    cloud_now = ((wx.get("current") or {}).get("cloud_cover")) if wx else None
    if cloud_now is not None:
        score = ((100 - cloud_now) / 100.0) * (1 - 0.6 * (mp_illum / 100.0)) * 100
        if sun_is_down is False:
            sg_cls, sg_lbl = "gray", "Daytime"
        elif score >= 65:
            sg_cls, sg_lbl = "green", "Excellent"
        elif score >= 45:
            sg_cls, sg_lbl = "green", "Good"
        elif score >= 25:
            sg_cls, sg_lbl = "yellow", "Fair"
        else:
            sg_cls, sg_lbl = "gray", "Poor"
        environment_html += f'<div class="metric"><span class="metric-label">Stargazing</span><span class="metric-value"><span class="pill {sg_cls}">{sg_lbl}</span></span></div>'

    # Solar production forecast: predicted kWh derived from the Open-Meteo daily
    # shortwave radiation we already fetch — kWh = (GHI MJ/m^2 / 3.6) * kWp * PR.
    # For flat-mounted panels GHI is the plane-of-array irradiance, so no tilt
    # transposition is needed. When configured, this refines the forecast tier
    # from predicted kWh vs measured consumption. (Replaces Forecast.Solar, whose
    # free tier read ~3x low for flat arrays.)
    solar_fc_html = ""
    rad_fc = ((wx or {}).get("daily") or {}).get("shortwave_radiation_sum") or []
    if SOLAR_KWP and rad_fc:
        today_kwh = (rad_fc[0] / 3.6) * SOLAR_KWP * SOLAR_PR if rad_fc[0] is not None else None
        tomo_kwh = (rad_fc[1] / 3.6) * SOLAR_KWP * SOLAR_PR if len(rad_fc) > 1 and rad_fc[1] is not None else None
        frac_left = sun_frac_left if sun_frac_left is not None else 1.0
        forward_kwh = max((today_kwh or 0) * frac_left, tomo_kwh or 0)   # today's remaining vs tomorrow
        if ac is not None and ac > 0:
            forecast_tier = "good" if forward_kwh >= 1.2 * ac else "fair" if forward_kwh >= 0.8 * ac else "poor"
        tomo_str = f"{tomo_kwh:.1f} kWh" if tomo_kwh is not None else "—"
        fc_cls = {"good": "green", "fair": "yellow", "poor": "red"}.get(forecast_tier, "gray")
        solar_fc_html = (f'<div class="metric"><span class="metric-label">Solar forecast</span>'
                         f'<span class="metric-value">{tomo_str} tomorrow <span class="pill {fc_cls}">{forecast_tier.title()}</span></span></div>')

    outlook_class, outlook_label = sustainability_outlook(
        inp["batt_present"], inp["soc_percent"], inp["charging_now"],
        inp["usable_wh"],
        ah * 1000 if ah is not None else None,
        ac * 1000 if ac is not None else None,
        forecast_tier,
    )

    return {
        "weather_html": weather_html, "environment_html": environment_html,
        "solar_fc_html": solar_fc_html,
        "outlook_class": outlook_class, "outlook_label": outlook_label,
    }


def placeholder_context(days):
    """Context for the instant page shell: every metric renders as a neutral
    loading placeholder with no DB or API work at all. dashboard.js then fills
    the page from /panels (local data) and /external (third-party lookups).
    Red pills so what's still loading catches the eye until real data lands."""
    pc, pl = "red", "…"     # placeholder pill class / label
    empty_charts = {k: [] for k in (
        "timestamps", "powers", "charge_powers", "voltage_timestamps",
        "voltage_values", "h20_days", "h20_values", "batt_times",
        "batt_soc_values", "batt_load_values", "batt_temp_values",
        "cons_days", "cons_values", "pi_times", "pi_temp_vals", "pi_mem_vals",
        "pi_fan_vals", "pi_load_vals", "pi_disk_vals",
    )}
    empty_charts.update({"h20_ymax": 1.6, "SOC_DANGER": 20, "FREEZE_F": 32,
                         "pi_mem_max": None, "pi_disk_max": None, "pi_load_max": None})
    return {
        "days": days, "MAX_DAYS": MAX_DAYS, "booting": True,
        # battery panel
        "soc_percent": "—", "soc_color": pc, "soc_label": pl,
        "latest_voltage": "—", "latest_voltage_class": pc, "latest_voltage_label": pl,
        "batt_per_display": "", "batt_feed_class": pc, "batt_feed_label": pl,
        "ps_class": pc, "ps_label": pl,
        "batt_temp_str": "—", "btemp_class": pc, "btemp_label": pl,
        "cell_class": pc, "cell_label": pl,
        "house_load_str": "—", "runtime_str": "—", "ttf_str": "—",
        # solar panel
        "status_color": pc, "status_text": "Loading…",
        "ctrl_class": pc, "ctrl_label": pl, "mode_class": pc, "mode_label": pl,
        "solar_now": "—", "charge_now_a_str": "—",
        "solar_class": pc, "solar_label": pl,
        "today_yield_str": "—", "latest_vpv_str": "—",
        "vpv_color": pc, "vpv_message": pl,
        "outlook_class": pc, "outlook_label": pl,
        # starlink panel
        "sl_status_class": pc, "sl_status_label": pl,
        "dish_class": pc, "dish_label": pl, "sl_streak_html": "",
        "sl_obs_str": "—", "sl_obs_class": pc, "sl_obs_label": pl,
        "sl_alert_class": pc, "sl_alert_label": pl,
        "sl_speed_str": "—", "sl_latency_str": "—",
        # pi health panel (the template's booting branch renders instead)
        "pi_status_row": None,
        # table + charts
        "table_rows": [], "chart_payload": empty_charts,
    }
