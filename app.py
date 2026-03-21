"""
DeltaPi Solar Monitor Server (Flask App)
----------------------------------------
This Flask application collects, stores, and visualizes solar charge controller data
and Raspberry Pi system health for off-grid solar monitoring systems such as those
used in RVs. It is designed to operate with a VE.Direct-compatible Victron charge
controller and one or more Raspberry Pi clients.

Key Features:
-------------
- Accepts POSTed VE.Direct solar data entries via `/log` and `/log/bulk`
- Stores solar data and Pi system health in an SQLite database
- Provides encrypted token-based access to the dashboard
- Single-page dashboard with time-series charts, system stats, and runtime estimates
- Implements HTTPS enforcement and token-based authorization
- Includes Flask-Limiter for rate limiting and abuse prevention
- Uses Fernet encryption for secure token generation
- LiFePO4 battery SOC estimation based on resting voltage
- Logs all major events to a file-based log (`server.log`)

Data Model:
-----------
SQLite DB stores:
- `logs`: timestamped solar readings including voltage, current, panel power, charge state, etc.
- `pi_status`: Raspberry Pi health reports including CPU temp, memory, disk, uptime, Wi-Fi, fan speed.

Routes Overview:
----------------
- `/log`           Accepts a single solar data point via POST
- `/log/bulk`      Accepts multiple solar data points in a single POST
- `/status`        Accepts system health stats from a Raspberry Pi
- `/`              Main dashboard with visualizations and summaries
- `/encrypt_days`  Encrypts number of days for secure token use

Dashboard Metrics:
------------------
- Latest/Average/Max battery voltage with status pills
- Estimated SOC (LiFePO4 voltage curve, resting only)
- Max/Average battery load
- Runtime estimates (current load, Starlink, Starlink + solar offset)
- Panel voltage with sunlight condition indicator
- Solar power over time (line chart)
- Battery voltage over time (line chart)
- Daily energy production H20 (line chart)
- Daily max solar power output H21 (bar chart)
- Pi system health: CPU temp, Wi-Fi signal, fan speed, disk, memory, uptime, OS updates
- Container status: disk usage, data retention days

Security:
---------
- All data ingestion routes require HTTPS and a valid bearer token
- Tokens used in dashboard are encrypted via Fernet to prevent tampering
- Rate limiting on all POST and sensitive GET endpoints

Intended Use:
-------------
This app is hosted on Render.com (free tier, ephemeral filesystem) and paired with
a field-deployed Raspberry Pi sending VE.Direct and system health data via bulk upload.
The Pi buffers data locally and uploads periodically based on trip/dormant mode.
"""

# ------------------ Imports ------------------ #
import os
import time as time_module
import json
import logging
import sqlite3
import shutil
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from cryptography.fernet import Fernet
from werkzeug.middleware.proxy_fix import ProxyFix
from flask import g
from dateutil.parser import parse as parse_date
from flask import has_request_context, request

# ------------------ App Setup ------------------ #
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)

# ------------------ Configuration ------------------ #
DB_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DB_DIR, "vedirect.db")
POST_SECRET = os.environ.get("POST_SECRET")
SERVER_LOG = os.path.join(DB_DIR, "server.log")
os.makedirs(DB_DIR, exist_ok=True)

FERNET_KEY = os.environ.get("FERNET_KEY")
fernet = Fernet(FERNET_KEY.encode()) if FERNET_KEY else None
MAX_DAYS = 60
_last_cleanup = 0

# ------------------ Rate Limiting ------------------ #
limiter = Limiter(key_func=get_remote_address)
limiter.init_app(app)

# ------------------ Charge State Mapping ------------------ #
CS_MAP = {
    "0": "Off",
    "1": "Low Power",
    "2": "Fault",
    "3": "Bulk",
    "4": "Absorption",
    "5": "Float"
}

# ------------------ DB Init ------------------ #
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                received TEXT,
                data TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pi_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT,
                timestamp TEXT,
                uptime TEXT,
                cpu_temp TEXT,
                disk TEXT,
                memory TEXT,
                ssid TEXT,
                wifi_signal TEXT,
                fan_speed TEXT,
                pi_name TEXT DEFAULT 'unknown',
                pi_os TEXT DEFAULT 'unknown',
                pi_updates TEXT DEFAULT 'unknown'
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs (timestamp)")

init_db()

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

def get_disk_status(path="/"):
    total, used, _ = shutil.disk_usage(path)
    percent = int((used / total) * 100)
    if percent < 70:
        return percent, "green", "Normal"
    elif percent < 90:
        return percent, "yellow", "High"
    else:
        return percent, "red", "Critical"

def cleanup_old_records():
    cutoff = datetime.utcnow() - timedelta(days=30)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    deleted = conn.execute("DELETE FROM logs WHERE timestamp < ?", (cutoff_str,)).rowcount
    conn.commit()
    if deleted:
        server_log("DB", f"Cleanup: removed {deleted} records older than 30 days", "info")

@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()

# ------------------ Helpers ------------------ #
def build_mode_datasets(modes, daily_mode_totals, days, blue_shades):
    datasets = []
    for idx, mode in enumerate(modes):
        data = [round(daily_mode_totals[day].get(mode, 0) / 60, 1) for day in days]
        datasets.append({"label": mode, "data": data, "backgroundColor": blue_shades[idx % len(blue_shades)]})
    return datasets

def clean_int(value):
    try:
        return int(str(value).replace("\x00", "").strip())
    except Exception:
        return 0

def estimate_runtime_wh(draw_w, battery_wh):
    if draw_w > 0.5:
        hours = battery_wh / draw_w
        return f"{hours:.1f} hours (~{hours/24:.1f} days)"
    return "Idle or charging"

def make_status_pill(value, thresholds):
    for threshold, (cls, label) in thresholds:
        if value < threshold:
            return cls, label
    return thresholds[-1][1]

def build_voltage_series(parsed):
    timestamps, values = [], []
    for ts, v, *_ in parsed:
        try:
            dt = datetime.fromisoformat(ts)
            timestamps.append(dt.strftime("%Y-%m-%d %H:%M"))
            values.append(round(v, 2) if v >= 11 else None)
        except Exception:
            continue
    return timestamps, values

def server_log(tag, message, level="info"):
    route = request.path if has_request_context() else "N/A"
    timestamp = datetime.utcnow().isoformat()
    entry = f"[{timestamp}] [{tag}] [ROUTE: {route}] {message}\n"
    try:
        with open(SERVER_LOG, "a") as f:
            f.write(entry)
    except Exception as e:
        logging.error(f"[Logger] Failed writing log file: {e}")
    getattr(logging, level)(f"[{tag}] [ROUTE: {route}] {message}")

def decrypt_token(token, min_days=1, max_days=MAX_DAYS):
    if not token:
        raise ValueError("Token missing")
    try:
        days = int(fernet.decrypt(token.encode()).decode())
    except Exception as e:
        raise ValueError(f"Token decryption failed: {e}")
    if not (min_days <= days <= max_days):
        raise ValueError(f"Token days out of range: {days}")
    return days

def estimate_soc(voltage):
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
    if soc >= 80: return ("green", "High")
    elif soc >= 50: return ("yellow", "Medium")
    else: return ("red", "Low")

# ------------------ Routes ------------------ #
@app.route("/log", methods=["POST"])
@limiter.limit("3 per minute")
def log():
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if not request.is_secure:
        server_log("POST", f"Insecure request rejected from {client_ip}", "warning")
        return jsonify({"error": "HTTPS required"}), 403
    if request.headers.get("Authorization", "") != f"Bearer {POST_SECRET}":
        server_log("POST", f"Rejected: Bad Auth from {client_ip}", "warning")
        return jsonify({"error": "Unauthorized"}), 403
    entry = request.get_json()
    if not entry:
        return jsonify({"error": "No data received"}), 400
    required_fields = ["V", "I", "PPV", "VPV", "timestamp"]
    if not all(field in entry for field in required_fields):
        return jsonify({"error": "Malformed data – missing required fields"}), 400
    timestamp = entry.get("timestamp", datetime.utcnow().isoformat())
    received = datetime.utcnow().isoformat()
    data_str = json.dumps(entry)
    try:
        conn = get_db()
        conn.execute("INSERT INTO logs (timestamp, received, data) VALUES (?, ?, ?)", (timestamp, received, data_str))
        conn.commit()
        global _last_cleanup
        if time_module.time() - _last_cleanup > 86400:
            cleanup_old_records()
            _last_cleanup = time_module.time()
        server_log("POST", f"Accepted data from {client_ip}", "info")
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        server_log("POST", f"DB error while logging from {client_ip}: {e}", "error")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/log/bulk", methods=["POST"])
@limiter.limit("2 per minute")
def bulk_log():
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if not request.is_secure:
        server_log("POST", f"Insecure bulk request from {client_ip}", "warning")
        return jsonify({"error": "HTTPS required"}), 403
    auth_header = request.headers.get("Authorization", "")
    if auth_header != f"Bearer {POST_SECRET}":
        server_log("POST", f"Rejected bulk request: Bad Auth from {client_ip}", "warning")
        return jsonify({"error": "Unauthorized"}), 403
    try:
        entries = request.get_json()
        if not isinstance(entries, list):
            raise ValueError("Expected a list of entries")
    except Exception as e:
        server_log("POST", f"Bad bulk JSON from {client_ip}: {e}", "warning")
        return jsonify({"error": "Invalid JSON"}), 400
    try:
        conn = get_db()
        timestamps_to_check = [e.get("timestamp") for e in entries if "timestamp" in e]
        placeholders = ",".join("?" for _ in timestamps_to_check)
        if timestamps_to_check:
            existing_ts = set(row[0] for row in conn.execute(f"SELECT timestamp FROM logs WHERE timestamp IN ({placeholders})", timestamps_to_check))
        else:
            existing_ts = set()
        inserted = 0
        for entry in entries:
            ts = entry.get("timestamp")
            if not ts or ts in existing_ts:
                continue
            received = datetime.utcnow().isoformat()
            data_str = json.dumps(entry)
            conn.execute("INSERT INTO logs (timestamp, received, data) VALUES (?, ?, ?)", (ts, received, data_str))
            inserted += 1
        conn.commit()
        global _last_cleanup
        if time_module.time() - _last_cleanup > 86400:
            cleanup_old_records()
            _last_cleanup = time_module.time()
        server_log("POST", f"Bulk insert from {client_ip}: {inserted} new entries", "info")
        return jsonify({"status": "ok", "inserted": inserted}), 200
    except Exception as e:
        server_log("POST", f"Bulk DB insert failed from {client_ip}: {e}", "error")
        return jsonify({"error": str(e)}), 500

@app.route("/encrypt_days")
@limiter.limit("10 per minute")
def encrypt_days():
    try:
        if not request.is_secure:
            server_log("GET", f"Insecure request rejected from {request.remote_addr}", "warning")
            return jsonify({"error": "HTTPS required"}), 403
        raw_days = request.args.get("days", "7")
        days = int(raw_days)
        if not (1 <= days <= 60):
            raise ValueError(f"Out-of-range: {days}")
        token = fernet.encrypt(str(days).encode()).decode()
        server_log("GET", f"Token generated for {days} day(s) from {request.remote_addr}", "info")
        return jsonify({"token": token})
    except Exception as e:
        server_log("GET", f"/encrypt_days error from {request.remote_addr}: {e}", "error")
        return jsonify({"token": ""}), 400

@app.route("/status", methods=["POST"])
@limiter.limit("2 per minute")
def pi_status():
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if not request.is_secure:
        server_log("POST", f"Insecure status update rejected from {client_ip}", "warning")
        return jsonify({"error": "HTTPS required"}), 403
    if request.headers.get("Authorization", "") != f"Bearer {POST_SECRET}":
        server_log("POST", f"Rejected: Bad Auth for /status from {client_ip}", "warning")
        return jsonify({"error": "Unauthorized"}), 403
    try:
        payload = request.get_json()
        required_fields = ["uptime", "cpu_temp", "disk", "memory", "ssid", "wifi_signal"]
        if not all(k in payload for k in required_fields):
            raise ValueError("Missing required status fields")
        conn = get_db()
        conn.execute(
            """INSERT INTO pi_status (ip, timestamp, uptime, cpu_temp, disk, memory, ssid, wifi_signal, fan_speed, pi_name, pi_os, pi_updates)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (client_ip, datetime.utcnow().isoformat(), payload["uptime"], payload["cpu_temp"],
             payload["disk"], payload["memory"], payload["ssid"], payload["wifi_signal"],
             payload.get("fan_speed", "unknown"), payload.get("pi_name", "unknown"),
             payload.get("pi_os", "unknown"), payload.get("pi_updates", "unknown"))
        )
        conn.commit()
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        server_log("POST", f"Status update failed from {client_ip}: {e}", "warning")
        return jsonify({"error": "Invalid payload"}), 400

# ------------------ Dashboard ------------------ #
@app.route("/", methods=["GET", "HEAD"])
def index():
    if request.method == "HEAD":
        return "", 200

    status_text = "Unknown"
    status_color = "#999"
    parsed = []
    now = datetime.utcnow()
    token = request.args.get("token")

    try:
        days = decrypt_token(token, min_days=1, max_days=MAX_DAYS) if token else 7
    except Exception:
        days = 7

    try:
        since = now - timedelta(days=days)
        conn = get_db()
        rows = conn.execute(
            "SELECT timestamp, received, data FROM logs WHERE timestamp >= ? ORDER BY timestamp DESC",
            (since.isoformat(),)
        ).fetchall()
    except Exception as db_err:
        server_log("GET", f"Database query failed: {db_err}", "error")
        return f"<p>Error reading database: {db_err}</p>"

    # Logger status
    if rows:
        try:
            latest_entry = json.loads(rows[0][2])
            last_ts = datetime.fromisoformat(latest_entry.get("timestamp", rows[0][0]))
        except Exception:
            last_ts = datetime.fromisoformat(rows[0][0])
        delta = datetime.utcnow() - last_ts
        if delta.total_seconds() < 600:
            status_color, status_text = "#28a745", "Receiving data"
        else:
            status_color, status_text = "#dc3545", f"No data in {int(delta.total_seconds() // 60)} min"
    else:
        status_color, status_text = "#dc3545", "No data available"

    # Pi status
    pi_status_row = None
    try:
        row = conn.execute(
            "SELECT ip, timestamp, uptime, cpu_temp, disk, memory, ssid, wifi_signal, fan_speed, pi_name, pi_os, pi_updates FROM pi_status ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        if row:
            pi_status_row = dict(zip(
                ["ip", "timestamp", "uptime", "cpu_temp", "disk", "memory", "ssid", "wifi_signal", "fan_speed", "pi_name", "pi_os", "pi_updates"], row
            ))
    except Exception as e:
        server_log("GET", f"Failed to fetch Pi status: {e}", "warning")

    # Parse logs
    for row in rows:
        try:
            data = json.loads(row["data"])
            ts = data.get("timestamp", row["timestamp"])
            ts_dt = datetime.fromisoformat(ts)
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
        except Exception as e:
            server_log("GET", f"Skipping row due to error: {e}", "warning")

    # Metrics
    parsed_chrono = []
    if not parsed:
        latest_voltage = average_voltage = max_voltage = "N/A"
        average_load = max_load = "N/A"
        latest_vpv = 0
        vpv_message = "No data available"
        table_data = []
    else:
        voltages = [p[1] for p in parsed]
        currents = [p[2] for p in parsed]
        loads = [round(v * a, 2) for v, a in zip(voltages, currents)]
        latest_voltage = f"{voltages[0]:.2f} V"
        average_voltage = f"{sum(voltages) / len(voltages):.2f} V"
        max_voltage = f"{max(voltages):.2f} V"
        average_load = f"{sum(loads) / len(loads):.2f} W"
        max_load = f"{max(loads):.2f} W"
        parsed_chrono = list(reversed(parsed))
        table_data = parsed_chrono[-5:] if len(parsed_chrono) > 5 else parsed_chrono
        latest_vpv = parsed[0][4]
        vpv_message = (
            "Nighttime" if latest_vpv < 5 else
            "Good Sunlight" if 16 <= latest_vpv <= 45 else
            "Over-Voltage" if latest_vpv > 45 else
            "Cloudy"
        )

    vpv_color = (
        "gray" if latest_vpv < 5 else
        "green" if 16 <= latest_vpv <= 45 else
        "red" if latest_vpv > 45 else
        "amber"
    )

    existing_days = conn.execute("SELECT COUNT(DISTINCT DATE(timestamp)) FROM logs").fetchone()[0]

    # Runtime estimates
    try:
        battery_wh = 200 * 12
        runtime_str = estimate_runtime_wh(voltages[0] * currents[0], battery_wh)
    except Exception:
        runtime_str = "N/A"

    try:
        starlink_watt_draw = 31
        starlink_runtime_str = estimate_runtime_wh(starlink_watt_draw, battery_wh)
        h21_today = parsed[0][9]
        solar_offset_wh = (h21_today / 1000) * 5000
        net_draw = starlink_watt_draw - (solar_offset_wh / 24)
        starlink_plus_solar_runtime_str = (
            estimate_runtime_wh(net_draw, battery_wh) if net_draw > 0
            else "Infinite (solar exceeds draw)"
        )
    except Exception:
        starlink_runtime_str = starlink_plus_solar_runtime_str = "N/A"

    # H20/H21 aggregation
    daily_h20 = defaultdict(float)
    daily_h21 = defaultdict(float)
    for row in reversed(parsed):
        try:
            ts = datetime.fromisoformat(row[0])
            day = ts.date().isoformat()
            daily_h20[day] = row[8]
            daily_h21[day] = max(daily_h21[day], row[9])
        except Exception:
            continue

    h20_days = sorted(daily_h20.keys())
    h20_values = [round(daily_h20[day], 2) for day in h20_days]
    h21_days = sorted(daily_h21.keys())
    h21_values = [round(daily_h21[day], 2) for day in h21_days]

    # Voltage chart
    voltage_timestamps, voltage_values = build_voltage_series(parsed_chrono)
    timestamps = [p[0] for p in reversed(parsed)]
    powers = [p[3] for p in reversed(parsed)]

    # Pi health pills
    try:
        last_checkin_dt = parse_date(pi_status_row['timestamp'])
        now = datetime.utcnow() if last_checkin_dt.tzinfo is None else datetime.now(timezone.utc)
        checkin_age_min = (now - last_checkin_dt).total_seconds() / 60
        checkin_class, checkin_label = make_status_pill(checkin_age_min, [
            (15, ("green", "recent")), (30, ("yellow", "moderate")), (float('inf'), ("red", "stale"))
        ])
        checkin_label = f"{int(checkin_age_min)} min ago"
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

    # SOC
    if parsed:
        voltage_float = voltages[0]
        if is_charging:
            soc_percent, soc_color, soc_label = None, "green", "Charging"
        else:
            soc_percent = estimate_soc(voltage_float)
            soc_color, soc_label = soc_pill(soc_percent)
    else:
        soc_percent, soc_color, soc_label = 0, "gray", "Unknown"

    # Disk status
    data_percent, data_class, data_label = get_disk_status(DB_DIR)

    # ==================== BUILD HTML ====================
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DeltaPi Solar Monitor</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link rel="apple-touch-icon" href="/static/icon.png">
    <link rel="icon" type="image/png" href="/static/icon.png">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-title" content="DeltaPi">
    <meta name="theme-color" content="#0a0a0a">
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'IBM Plex Sans', sans-serif;
            font-size: 13px;
            background: #0a0a0a;
            color: #e0e0e0;
            height: 100vh;
            overflow: hidden;
            display: flex;
            flex-direction: column;
        }}
        .header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 8px 16px;
            background: #111;
            border-bottom: 1px solid #222;
            flex-shrink: 0;
        }}
        .header h1 {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 15px;
            font-weight: 700;
            color: #4fc3f7;
            letter-spacing: 1px;
        }}
        .header form {{
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .header input[type="number"] {{
            width: 48px;
            background: #1a1a1a;
            border: 1px solid #333;
            color: #e0e0e0;
            padding: 3px 6px;
            border-radius: 4px;
            font-family: 'JetBrains Mono', monospace;
            font-size: 12px;
        }}
        .header button {{
            background: #4fc3f7;
            color: #0a0a0a;
            border: none;
            padding: 4px 12px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
            cursor: pointer;
            font-family: 'IBM Plex Sans', sans-serif;
        }}
        .header button:hover {{ background: #81d4fa; }}
        .header label {{ font-size: 11px; color: #888; }}

        .dashboard {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            grid-template-rows: auto 1fr 1fr auto;
            gap: 6px;
            padding: 6px;
            flex: 1;
            overflow: hidden;
        }}

        .panel {{
            background: #111;
            border: 1px solid #1e1e1e;
            border-radius: 6px;
            padding: 10px 12px;
            overflow: hidden;
        }}
        .panel h2 {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 11px;
            font-weight: 600;
            color: #4fc3f7;
            text-transform: uppercase;
            letter-spacing: 1.5px;
            margin-bottom: 6px;
        }}
        .metric {{
            display: flex;
            justify-content: space-between;
            padding: 2px 0;
            font-size: 12px;
            line-height: 1.5;
        }}
        .metric-label {{ color: #888; }}
        .metric-value {{ color: #e0e0e0; font-family: 'JetBrains Mono', monospace; font-weight: 600; font-size: 12px; }}

        .pill {{
            display: inline-block;
            padding: 1px 8px;
            border-radius: 999px;
            color: #fff;
            font-weight: 600;
            font-size: 10px;
            line-height: 1.6;
            vertical-align: middle;
        }}
        .pill.green {{ background: #2e7d32; }}
        .pill.yellow {{ background: #f9a825; color: #1a1a1a; }}
        .pill.red {{ background: #c62828; }}
        .pill.gray {{ background: #424242; }}
        .pill.orange {{ background: #e65100; }}

        .chart-panel {{
            background: #111;
            border: 1px solid #1e1e1e;
            border-radius: 6px;
            padding: 6px 8px;
            display: flex;
            flex-direction: column;
        }}
        .chart-panel h2 {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 10px;
            font-weight: 600;
            color: #4fc3f7;
            text-transform: uppercase;
            letter-spacing: 1.5px;
            margin-bottom: 4px;
            flex-shrink: 0;
        }}
        .chart-wrap {{
            flex: 1;
            position: relative;
            min-height: 0;
        }}
        .chart-wrap canvas {{
            position: absolute;
            top: 0; left: 0;
            width: 100% !important;
            height: 100% !important;
        }}

        .table-panel {{
            grid-column: 1 / -1;
            background: #111;
            border: 1px solid #1e1e1e;
            border-radius: 6px;
            padding: 8px 12px;
            overflow-x: auto;
            flex-shrink: 0;
        }}
        .table-panel h2 {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 10px;
            font-weight: 600;
            color: #4fc3f7;
            text-transform: uppercase;
            letter-spacing: 1.5px;
            margin-bottom: 4px;
        }}
        table {{ width: 100%; border-collapse: collapse; }}
        th {{
            font-size: 10px;
            font-weight: 600;
            color: #666;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            padding: 3px 6px;
            text-align: center;
            border-bottom: 1px solid #222;
        }}
        td {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 11px;
            padding: 3px 6px;
            text-align: center;
            color: #bbb;
            border-bottom: 1px solid #1a1a1a;
        }}
    </style>
</head>
<body>

<div class="header">
    <h1>DELTAPI SOLAR MONITOR</h1>
    <form method="get" onsubmit="event.preventDefault(); encryptAndSubmit();">
        <label>Last</label>
        <input type="number" id="daysInput" value="{days}" min="1" max="60">
        <label>days</label>
        <input type="hidden" id="tokenInput" name="token">
        <button type="submit">Update</button>
    </form>
</div>

<div class="dashboard">
    <!-- Solar Summary -->
    <div class="panel">
        <h2>Solar System</h2>
        <div class="metric"><span class="metric-label">Status</span><span class="metric-value"><span class="pill" style="background:{status_color};">{status_text}</span></span></div>
        <div class="metric"><span class="metric-label">Voltage</span><span class="metric-value">{latest_voltage} <span class="pill {latest_voltage_class}">{latest_voltage_label}</span></span></div>
        <div class="metric"><span class="metric-label">Avg / Max V</span><span class="metric-value">{average_voltage} / {max_voltage}</span></div>
        <div class="metric"><span class="metric-label">SOC</span><span class="metric-value">{soc_percent}% <span class="pill" style="background:{soc_color};">{soc_label}</span></span></div>
        <div class="metric"><span class="metric-label">Load (avg/max)</span><span class="metric-value">{average_load} / {max_load}</span></div>
        <div class="metric"><span class="metric-label">Runtime Est.</span><span class="metric-value">{runtime_str}</span></div>
        <div class="metric"><span class="metric-label">Starlink</span><span class="metric-value">{starlink_runtime_str}</span></div>
        <div class="metric"><span class="metric-label">Starlink+Solar</span><span class="metric-value">{starlink_plus_solar_runtime_str}</span></div>
        <div class="metric"><span class="metric-label">Panel (VPV)</span><span class="metric-value">{latest_vpv:.2f} V <span class="pill" style="background:{vpv_color};">{vpv_message}</span></span></div>
    </div>

    <!-- Pi Health -->
    <div class="panel">"""

    if pi_status_row:
        html += f"""
        <h2>Pi Health — {pi_status_row['pi_name'].upper()}</h2>
        <div class="metric"><span class="metric-label">OS</span><span class="metric-value">{pi_status_row.get('pi_os')}</span></div>
        <div class="metric"><span class="metric-label">Uptime</span><span class="metric-value">{pi_status_row['uptime']}</span></div>
        <div class="metric"><span class="metric-label">Last Check-in</span><span class="metric-value"><span class="pill {checkin_class}">{checkin_label}</span></span></div>
        <div class="metric"><span class="metric-label">CPU / Fan</span><span class="metric-value">{pi_status_row['cpu_temp']} <span class="pill {temp_class}">{temp_label}</span> {pi_status_row.get("fan_speed", "?")} <span class="pill {fan_class}">{fan_label}</span></span></div>
        <div class="metric"><span class="metric-label">Updates</span><span class="metric-value">{pi_status_row.get('pi_updates')} <span class="pill {updates_class}">{updates_label}</span></span></div>
        <div class="metric"><span class="metric-label">Mem / Disk</span><span class="metric-value">{pi_status_row['memory']} / {pi_status_row['disk']}</span></div>
        <div class="metric"><span class="metric-label">Wi-Fi</span><span class="metric-value">{pi_status_row.get("ssid", "?")} {pi_status_row.get("wifi_signal", "?")} <span class="pill {wifi_class}">{wifi_label}</span></span></div>
        <div class="metric"><span class="metric-label">Container</span><span class="metric-value">{data_percent}% <span class="pill {data_class}">{data_label}</span> — {existing_days}d avail</span></div>"""
    else:
        html += """
        <h2>Pi Health</h2>
        <p style="color:#666; font-size:12px;">No Pi status data available.</p>"""

    html += f"""
    </div>

    <!-- Chart: Solar Power -->
    <div class="chart-panel">
        <h2>Solar Power (W)</h2>
        <div class="chart-wrap"><canvas id="chartPower"></canvas></div>
    </div>

    <!-- Chart: Battery Voltage -->
    <div class="chart-panel">
        <h2>Battery Voltage (V)</h2>
        <div class="chart-wrap"><canvas id="chartVoltage"></canvas></div>
    </div>

    <!-- Chart: Daily Energy H20 -->
    <div class="chart-panel">
        <h2>Daily Energy (kWh)</h2>
        <div class="chart-wrap"><canvas id="chartH20"></canvas></div>
    </div>

    <!-- Chart: Daily Max Power H21 -->
    <div class="chart-panel">
        <h2>Daily Peak Power (W)</h2>
        <div class="chart-wrap"><canvas id="chartH21"></canvas></div>
    </div>

    <!-- Latest Readings Table -->
    <div class="table-panel">
        <h2>Latest Readings</h2>
        <table>
            <thead><tr>
                <th>Time</th><th>Voltage</th><th>Current</th><th>Power</th>
                <th>Panel V</th><th>Load</th><th>Mode</th><th>H20</th><th>H21</th>
            </tr></thead>
            <tbody>"""

    for ts, v, i, ppv, vpv, load, cs, err, h20, h21 in reversed(table_data):
        power = round(v * i, 2)
        html += f"""
                <tr><td>{ts}</td><td>{v}</td><td>{i}</td><td>{power}</td>
                <td>{vpv}</td><td>{load}</td><td>{cs}</td><td>{h20}</td><td>{h21}</td></tr>"""

    html += f"""
            </tbody>
        </table>
    </div>
</div>

<script>
function encryptAndSubmit() {{
    let days = document.getElementById('daysInput').value;
    fetch('/encrypt_days?days=' + encodeURIComponent(days))
        .then(res => res.json())
        .then(data => {{
            document.getElementById('tokenInput').value = data.token;
            document.querySelector('form').submit();
        }})
        .catch(() => alert('Encryption error'));
}}

const chartOpts = (yMin, yMax, stepSize) => ({{
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    plugins: {{ legend: {{ display: false }} }},
    elements: {{ point: {{ radius: 0 }}, line: {{ borderWidth: 1.5 }} }},
    scales: {{
        x: {{ display: false }},
        y: {{ min: yMin, max: yMax, ticks: {{ stepSize: stepSize, font: {{ size: 9 }}, color: '#555' }}, grid: {{ color: '#1a1a1a' }} }}
    }}
}});

// Solar Power
new Chart(document.getElementById('chartPower'), {{
    type: 'line',
    data: {{
        labels: {json.dumps(timestamps)},
        datasets: [{{ data: {json.dumps(powers)}, borderColor: '#ff9800', fill: false, tension: 0.1 }}]
    }},
    options: chartOpts(0, 305, 50)
}});

// Battery Voltage
new Chart(document.getElementById('chartVoltage'), {{
    type: 'line',
    data: {{
        labels: {json.dumps(voltage_timestamps)},
        datasets: [{{ data: {json.dumps(voltage_values)}, borderColor: '#4fc3f7', backgroundColor: 'rgba(79,195,247,0.08)', fill: true, tension: 0.3, spanGaps: false }}]
    }},
    options: chartOpts(12.5, 14.6, 0.5)
}});

// Daily Energy H20
new Chart(document.getElementById('chartH20'), {{
    type: 'line',
    data: {{
        labels: {json.dumps(h20_days)},
        datasets: [
            {{ data: {json.dumps(h20_values)}, borderColor: '#26a69a', backgroundColor: 'rgba(38,166,154,0.08)', fill: true, tension: 0.2, pointRadius: 2 }},
            {{ data: Array({len(h20_days)}).fill(1.5), borderColor: '#c62828', borderDash: [4,3], fill: false, pointRadius: 0 }},
            {{ data: Array({len(h20_days)}).fill(0.14), borderColor: '#f9a825', borderDash: [3,3], fill: false, pointRadius: 0 }}
        ]
    }},
    options: chartOpts(0, 1.6, 0.4)
}});

// Daily Max Power H21
new Chart(document.getElementById('chartH21'), {{
    type: 'bar',
    data: {{
        labels: {json.dumps(h21_days)},
        datasets: [{{ data: {json.dumps(h21_values)}, backgroundColor: 'rgba(102,187,106,0.5)', borderColor: '#66bb6a', borderWidth: 1 }}]
    }},
    options: chartOpts(0, 300, 50)
}});
</script>
</body>
</html>"""

    return html

if __name__ == "__main__":
    app.run()