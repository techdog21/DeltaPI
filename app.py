"""
DeltaPi Solar Monitor Server (Flask App)
----------------------------------------

This Flask application collects, stores, and visualizes solar charge controller data and Raspberry Pi system health for off-grid solar monitoring systems such as those used in RVs. 
It is designed to operate with a VE.Direct-compatible Victron charge controller and one or more Raspberry Pi clients.

Key Features:
-------------
• Accepts POSTed VE.Direct solar data entries via `/log` and `/log/bulk`
• Stores solar data and Pi system health in Turso (libSQL cloud database)
• Provides encrypted token-based access to dashboards, exports, and data
• Offers a secure dashboard at `/` with time-series charts, system stats, and runtime estimates
• Supports CSV export (`/export.csv`), Pi health reports (`/status`), and exploratory views (`/explore`, `/pi_explore`)
• Implements HTTPS enforcement and token-based authorization
• Includes Flask-Limiter for rate limiting and abuse prevention
• Uses Fernet encryption for secure token generation
• Logs all major events to a rotating file-based log (`server.log`)

Data Model:
-----------
Turso (libSQL) DB stores:
- `logs`: timestamped solar readings including voltage, current, panel power, charge state, etc.
- `pi_status`: Raspberry Pi health reports including CPU temp, memory, disk, uptime, Wi-Fi, fan speed.

Routes Overview:
----------------
• `/log`           Accepts a single solar data point via POST
• `/log/bulk`      Accepts multiple solar data points in a single POST
• `/status`        Accepts system health stats from a Raspberry Pi
• `/`              Main dashboard with visualizations and summaries
• `/export.csv`    Exports solar data to CSV (token required)
• `/encrypt_days`  Encrypts number of days for secure token use
• `/debug`         Shows latest 10 raw solar entries (token required)
• `/explore`       Client-filterable solar data table (up to 10k rows)
• `/pi_explore`    Client-filterable Pi health data table

Security:
---------
• All data ingestion and export routes require HTTPS and a valid bearer token
• Tokens used in dashboards and exports are encrypted to prevent tampering

Intended Use:
-------------
This app is intended to be hosted on a cloud platform (e.g., Render.com free tier) and paired with a field-deployed Raspberry Pi sending VE.Direct and system health data. 
Database is hosted on Turso (libSQL) — no persistent disk required.
"""

# ------------------ Imports ------------------ #
import os
import time as time_module
import json
import logging
import shutil
from datetime import datetime, time, timedelta, timezone
from collections import defaultdict
from flask import Flask, request, jsonify, g
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from cryptography.fernet import Fernet
from werkzeug.middleware.proxy_fix import ProxyFix
from dateutil.parser import parse as parse_date
from flask import has_request_context, request
import libsql_client

# ------------------ App Setup ------------------ #
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)

# ------------------ Configuration ------------------ #
# Turso database configuration (replaces local SQLite)
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL")  # e.g. libsql://deltapi-yourorg.turso.io
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

# Server log — still local (ephemeral, rebuilds on deploy)
LOG_DIR = "/tmp/deltapi"
SERVER_LOG = os.path.join(LOG_DIR, "server.log")
os.makedirs(LOG_DIR, exist_ok=True)

POST_SECRET = os.environ.get("POST_SECRET", "deltapiproject123")

# Fernet key for encrypting tokens
FERNET_KEY = os.environ.get("FERNET_KEY")
fernet = Fernet(FERNET_KEY.encode()) if FERNET_KEY else None
MAX_DAYS = 60

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

# ------------------ DB Connection Helper ------------------ #
def get_db():
    """
    Returns a synchronous libsql_client for the current request context.
    Reuses the same client within a single request via Flask's g object.
    """
    if 'db' not in g:
        g.db = libsql_client.create_client_sync(
            url=TURSO_DATABASE_URL,
            auth_token=TURSO_AUTH_TOKEN
        )
    return g.db

@app.teardown_appcontext
def close_db(exception):
    """
    Closes the libsql client at the end of each request.
    """
    db = g.pop('db', None)
    if db is not None:
        db.close()

# ------------------ DB Init ------------------ #
def init_db():
    """
    Initializes the Turso database with required tables if they do not exist.
    Uses a standalone client (not request-scoped) since this runs at startup.
    """
    with libsql_client.create_client_sync(
        url=TURSO_DATABASE_URL,
        auth_token=TURSO_AUTH_TOKEN
    ) as client:
        # Logs Table
        client.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                received TEXT,
                data TEXT
            )
        """)

        # Pi Status Table
        client.execute("""
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

        # Index on timestamp for faster queries
        client.execute("CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs (timestamp)")


# Initialize tables at startup
init_db()

# ------------------ Helper: Row Access Wrapper ------------------ #
def row_to_dict(row, columns):
    """
    Converts a libsql_client result row (tuple) + column names into a dict.
    This replaces sqlite3.Row dict-like access throughout the app.
    """
    return dict(zip(columns, row))

# ------------------ Helper: Mode Datasets ------------------ #
def build_mode_datasets(modes, daily_mode_totals, days, blue_shades):
    '''
    Constructs Chart.js-compatible datasets for a stacked bar chart of solar charge modes.
    '''
    datasets = []
    for idx, mode in enumerate(modes):
        data = [
            round(daily_mode_totals[day].get(mode, 0) / 60, 1)
            for day in days
        ]
        datasets.append({
            "label": mode,
            "data": data,
            "backgroundColor": blue_shades[idx % len(blue_shades)]
        })
    return datasets

# ------------------ Helper: Clean Int Conversion ------------------ #
def clean_int(value):
    """
    Safely converts a value to an integer, stripping null bytes and whitespace.
    """
    try:
        return int(str(value).replace("\x00", "").strip())
    except:
        return 0

# ------------------ Helper: Runtime Estimation ------------------ #
def estimate_runtime_wh(draw_w, battery_wh):
    """
    Estimates how long a battery can run at the current power draw.
    """
    if draw_w > 0.5:
        hours = battery_wh / draw_w
        return f"{hours:.1f} hours (~{hours/24:.1f} days)"
    return "Idle or charging"

# ------------------ Helper: Status Pill ------------------ #
def make_status_pill(value, thresholds):
    """
    Assigns a status class and label based on numeric thresholds.
    """
    for threshold, (cls, label) in thresholds:
        if value < threshold:
            return cls, label
    return thresholds[-1][1]

# ------------------ Helper: Voltage Series ------------------ #
def build_voltage_series(parsed):
    """
    Extracts and formats a voltage time series from parsed solar data.
    """
    timestamps, values = [], []
    for ts, v, *_ in parsed:
        try:
            dt = datetime.fromisoformat(ts)
            timestamps.append(dt.strftime("%Y-%m-%d %H:%M"))
            values.append(round(v, 2) if v >= 11 else None)
        except:
            continue
    return timestamps, values

# ------------------ Helper: Server Log ------------------ #
def server_log(tag, message, level="info"):
    """
    Logs a message with a timestamp, a custom tag, and the current request route.
    """
    route = request.path if has_request_context() else "N/A"
    timestamp = datetime.utcnow().isoformat()
    entry = f"[{timestamp}] [{tag}] [ROUTE: {route}] {message}\n"

    try:
        with open(SERVER_LOG, "a") as f:
            f.write(entry)
    except Exception as e:
        logging.error(f"[Logger] Failed writing log file: {e}")

    getattr(logging, level)(f"[{tag}] [ROUTE: {route}] {message}")

# ------------------ Helper: Decrypt Token ------------------ #
def decrypt_token(token, min_days=1, max_days=MAX_DAYS):
    """
    Safely decrypts a Fernet token and returns the number of days encoded.
    """
    if not token:
        raise ValueError("Token missing")

    try:
        days = int(fernet.decrypt(token.encode()).decode())
    except Exception as e:
        raise ValueError(f"Token decryption failed: {e}")

    if not (min_days <= days <= max_days):
        raise ValueError(f"Token days out of range: {days}")

    return days

def get_render_deploy_status():
    import requests as req_lib

    api_key = os.getenv("RENDER_API_KEY")
    service_id = os.getenv("SERVICE_ID")
    if not api_key or not service_id:
        return ("gray", "Unknown")

    url = f"https://api.render.com/v1/services/{service_id}/deploys"
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        r = req_lib.get(url, headers=headers, timeout=5)
        r.raise_for_status()
        deploys = r.json()
        for deploy in deploys:
            if deploy.get("status") in ("live", "ready", "successful"):
                dt = datetime.fromisoformat(deploy["updatedAt"].replace("Z", "+00:00"))
                age_minutes = (datetime.utcnow() - dt).total_seconds() / 60

                deploy_class, deploy_label = make_status_pill(age_minutes, [
                    (10080, ("green", "Fresh")),
                    (20160, ("yellow", "Recent")),
                    (43200, ("orange", "Aged")),
                    (float('inf'), ("red", "Stale"))
                ])

                deploy_time = dt.strftime("%Y-%m-%d %H:%M UTC")
                return (deploy_class, f"{deploy_time} ({deploy_label})")
        return ("gray", "No live deploy")
    except Exception as e:
        return ("gray", "Error")

def estimate_soc(voltage):
    """
    Estimates SOC for LiFePO4 batteries based on resting voltage.
    """
    if voltage >= 13.6:
        return 100
    elif voltage >= 13.4:
        return 95
    elif voltage >= 13.2:
        return 90
    elif voltage >= 13.1:
        return 85
    elif voltage >= 13.0:
        return 75
    elif voltage >= 12.9:
        return 65
    elif voltage >= 12.8:
        return 55
    elif voltage >= 12.7:
        return 45
    elif voltage >= 12.6:
        return 35
    elif voltage >= 12.4:
        return 25
    elif voltage >= 12.2:
        return 15
    else:
        return 5

def soc_pill(soc):
    if soc >= 80:
        return ("green", "High")
    elif soc >= 50:
        return ("yellow", "Medium")
    else:
        return ("red", "Low")

def get_disk_status(path="/"):
    """
    Get disk usage for the given path.
    """
    total, used, free = shutil.disk_usage(path)
    percent = int((used / total) * 100)

    if percent < 70:
        return percent, "green", "Normal"
    elif percent < 90:
        return percent, "yellow", "High"
    else:
        return percent, "red", "Critical"

# ------------------ POST: Single Log Entry ------------------ #
@app.route("/log", methods=["POST"])
@limiter.limit("3 per minute")
def log():
    '''
    Accepts a single VE.Direct solar data entry via POST and stores it in the database.
    '''
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
        client = get_db()
        client.execute(
            "INSERT INTO logs (timestamp, received, data) VALUES (?, ?, ?)",
            [timestamp, received, data_str]
        )
        server_log("POST", f"Accepted data from {client_ip}", "info")
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        server_log("POST", f"DB error while logging from {client_ip}: {e}", "error")
        return jsonify({"error": "Internal server error"}), 500

# ------------------ POST: Bulk Log Entries ------------------ #
@app.route("/log/bulk", methods=["POST"])
@limiter.limit("2 per minute")
def bulk_log():
    '''
    Accepts a bulk POST of VE.Direct solar data entries.
    '''
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)

    if not request.is_secure:
        server_log("POST", f"Insecure bulk request from {client_ip}", "warning")
        return jsonify({"error": "HTTPS required"}), 403

    auth_header = request.headers.get("Authorization", "")
    if auth_header != f"Bearer {POST_SECRET}":
        server_log("POST", f"Rejected bulk request: Bad Auth from {client_ip}, header: {auth_header}", "warning")
        return jsonify({"error": "Unauthorized"}), 403

    try:
        entries = request.get_json()
        if not isinstance(entries, list):
            raise ValueError("Expected a list of entries")
    except Exception as e:
        server_log("POST", f"Bad bulk JSON from {client_ip}: {e}", "warning")
        return jsonify({"error": "Invalid JSON"}), 400

    try:
        client = get_db()

        # Get existing timestamps to deduplicate
        timestamps_to_check = [e.get("timestamp") for e in entries if "timestamp" in e]

        existing_ts = set()
        if timestamps_to_check:
            # libsql_client doesn't support IN (?) with list expansion the same way
            # Process in batches to check for existing timestamps
            for ts in timestamps_to_check:
                rs = client.execute("SELECT timestamp FROM logs WHERE timestamp = ?", [ts])
                if rs.rows:
                    existing_ts.add(ts)

        inserted = 0
        for entry in entries:
            ts = entry.get("timestamp")
            if not ts or ts in existing_ts:
                continue

            received = datetime.utcnow().isoformat()
            data_str = json.dumps(entry)

            client.execute(
                "INSERT INTO logs (timestamp, received, data) VALUES (?, ?, ?)",
                [ts, received, data_str]
            )
            inserted += 1

        server_log("POST", f"Bulk insert from {client_ip}: {inserted} new entries", "info")
        return jsonify({"status": "ok", "inserted": inserted}), 200
    except Exception as e:
        server_log("POST", f"Bulk DB insert failed from {client_ip}: {e}", "error")
        return jsonify({"error": str(e)}), 500

# ------------------ GET: Encrypt Days ------------------ #
@app.route("/encrypt_days")
@limiter.limit("10 per minute")
def encrypt_days():
    """
    Encrypt the number of days requested by the user for secure URL usage.
    """
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

# ------------------ GET: CSV Export ------------------ #
@app.route("/export.csv")
@limiter.limit("5 per minute")
def export_csv():
    """
    Export the last N days of VE.Direct data in CSV format.
    """
    try:
        if not request.is_secure:
            server_log("POST", f"CSV export blocked: Insecure request from {request.remote_addr}", "warning")
            return "HTTPS required", 403

        token = request.args.get("token")
        days = decrypt_token(token)
    except Exception as e:
        server_log("POST", f"CSV export failed: Invalid or missing token from {request.remote_addr} - {e}", "error")
        days = 7

    since = datetime.utcnow() - timedelta(days=days)
    output = "timestamp,voltage,current,ppv,vpv,load,charge_mode,error,h20\n"

    try:
        client = get_db()
        rs = client.execute(
            "SELECT timestamp, data FROM logs WHERE timestamp >= ? ORDER BY timestamp DESC",
            [since.isoformat()]
        )
        rows = rs.rows

    except Exception as db_err:
        server_log("POST", f"CSV export failed: DB read error from {request.remote_addr} - {db_err}", "error")
        return "Error reading database", 500

    for row in rows:
        try:
            data = json.loads(row[1])
            output += ",".join([
                data.get("timestamp", row[0]),
                f"{int(data.get('V', 0)) / 1000:.2f}",
                f"{int(data.get('I', 0)) / 1000:.2f}",
                str(int(data.get("PPV", 0))),
                f"{int(data.get('VPV', 0)) / 1000:.2f}",
                str(data.get("LOAD", "N/A")),
                CS_MAP.get(str(data.get("CS", "0")), f"Unknown ({data.get('CS')})"),
                str(data.get("ERR", "0")),
                f"{int(data.get('H20', 0)) / 100:.2f}"
            ]) + "\n"
        except Exception as row_err:
            server_log("POST", f"CSV row skipped from {request.remote_addr}: {row_err}", "warning")
            continue

    server_log("POST", f"CSV export successful for {request.remote_addr} - Days: {days}", "info")
    return output, 200, {
        'Content-Type': 'text/csv',
        'Content-Disposition': f'attachment; filename=vedirect_last_{days}_days.csv'
    }

# ------------------ GET: Debug Page ------------------ #
@app.route("/debug")
def debug():
    """
    Render a simple debug page showing the 10 most recent entries in the logs table.
    """
    token = request.args.get("token")
    if not token:
        server_log("GET", f"/debug access denied from {request.remote_addr}: missing token", "warning")
        return "<p>Unauthorized: token missing.</p>", 403

    try:
        days = decrypt_token(token, min_days=1)
    except Exception as e:
        server_log("GET", f"/debug access denied from {request.remote_addr}: {e}", "warning")
        return "<p>Unauthorized or invalid token.</p>", 403

    try:
        client = get_db()
        rs = client.execute("SELECT timestamp, data FROM logs ORDER BY timestamp DESC LIMIT 10")
        rows = rs.rows
    except Exception as db_err:
        server_log("GET", f"/debug DB read error from {request.remote_addr}: {db_err}", "error")
        return "<p>Error reading database.</p>", 500

    server_log("GET", f"/debug accessed successfully from {request.remote_addr}", "info")

    html = "<html><head><title>Debug Logs</title></head><body>"
    html += "<h2>Latest Entries (Most Recent First)</h2><ul style='font-family: monospace;'>"

    for row in rows:
        ts, data = row[0], row[1]
        escaped_data = str(data).replace("<", "&lt;").replace(">", "&gt;")
        html += f"<li><pre>{ts} - {escaped_data}</pre></li>"

    html += "</ul><p><a href='/'>Back to Dashboard</a></p></body></html>"
    return html

# ------------------ POST: Pi Status ------------------ #
@app.route("/status", methods=["POST"])
@limiter.limit("2 per minute")
def pi_status():
    """
    Accepts a POST request from a Raspberry Pi containing system health statistics.
    """
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)

    if not request.is_secure:
        server_log("POST", f"Insecure status update rejected from {client_ip}", "warning")
        return jsonify({"error": "HTTPS required"}), 403

    if request.headers.get("Authorization", "") != f"Bearer {POST_SECRET}":
        server_log("POST", f"Rejected: Bad Auth for /status from {client_ip}", "warning")
        return jsonify({"error": "Unauthorized"}), 403

    try:
        payload = request.get_json()
        server_log("POST", f"Status payload: {json.dumps(payload)}", "info")

        required_fields = ["uptime", "cpu_temp", "disk", "memory", "ssid", "wifi_signal"]
        if not all(k in payload for k in required_fields):
            raise ValueError("Missing required status fields")

        fan_speed = payload.get("fan_speed", "unknown")
        pi_name = payload.get("pi_name", "unknown")
        pi_os = payload.get("pi_os", "unknown")
        pi_updates = payload.get("pi_updates", "unknown")

        client = get_db()
        client.execute(
            """INSERT INTO pi_status 
            (ip, timestamp, uptime, cpu_temp, disk, memory, ssid, wifi_signal, fan_speed, pi_name, pi_os, pi_updates)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                client_ip,
                datetime.utcnow().isoformat(),
                payload["uptime"],
                payload["cpu_temp"],
                payload["disk"],
                payload["memory"],
                payload["ssid"],
                payload["wifi_signal"],
                fan_speed,
                pi_name,
                pi_os,
                pi_updates
            ]
        )
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        server_log("POST", f"Status update failed from {client_ip}: {e}", "warning")
        return jsonify({"error": "Invalid payload"}), 400

# ------------------ GET: Explore Solar Data ------------------ #
@app.route("/explore")
def explore():
    """
    Explore Data page — shows up to 10,000 recent log entries with client-side filtering.
    """
    client = get_db()
    rs = client.execute("SELECT timestamp, received, data FROM logs ORDER BY timestamp DESC LIMIT 10000")

    parsed = []
    for row in rs.rows:
        try:
            data = json.loads(row[2])

            v = clean_int(data.get("V", 0)) / 1000
            i = clean_int(data.get("I", 0)) / 1000
            ppv = clean_int(data.get("PPV", 0))
            vpv = clean_int(data.get("VPV", 0)) / 1000
            load = data.get("LOAD", "N/A")
            cs = CS_MAP.get(str(data.get("CS", "0")), f"Unknown ({data.get('CS')})")
            err = data.get("ERR", "0")
            h20 = clean_int(data.get("H20", 0)) / 100
            h21 = clean_int(data.get("H21", 0))

            ts = data.get("timestamp", row[0])
            parsed.append((ts, v, i, round(v * i, 2), ppv, vpv, load, cs, err, h20, h21))
        except:
            continue

    html = """
    <html>
    <head>
        <title>Explore Data</title>
        <link rel="stylesheet" href="https://cdn.datatables.net/1.13.6/css/jquery.dataTables.min.css">
        <script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
        <script src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.min.js"></script>
        <style>
            body { font-family: sans-serif; padding: 1em; }
            table { width: 100%; border-collapse: collapse; margin-top: 1em; }
            th, td { padding: 6px; border: 1px solid #ccc; text-align: center; }
            th { background: #f0f0f0; }
            input { width: 100%; box-sizing: border-box; }
        </style>
    </head>
    <body>
        <h2>Explore Solar Data (Latest 10,000 Entries)</h2>
        <p><a href="/">Back to Dashboard</a></p>
        <table id="explore-table" class="display">
            <thead>
                <tr>
                    <th>Time (TS)</th>
                    <th>Battery Voltage (V)</th>
                    <th>Battery Current (A)</th>
                    <th>Battery Load (W)(I)</th>
                    <th>Solar Power (W)(LOAD)</th>
                    <th>Panel Voltage (V)(PPV)</th>
                    <th>Load Output (V)(VPV)</th>
                    <th>Charge Mode (CS)</th>
                    <th>Error Code (ERR)</th>
                    <th>Energy Today kWh (H20)</th>
                    <th>Max Solar Power (H21)</th>
                </tr>
                <tr>
                    <th><input type="text" placeholder="Filter"/></th>
                    <th><input type="text" placeholder="Filter"/></th>
                    <th><input type="text" placeholder="Filter"/></th>
                    <th><input type="text" placeholder="Filter"/></th>
                    <th><input type="text" placeholder="Filter"/></th>
                    <th><input type="text" placeholder="Filter"/></th>
                    <th><input type="text" placeholder="Filter"/></th>
                    <th><input type="text" placeholder="Filter"/></th>
                    <th><input type="text" placeholder="Filter"/></th>
                    <th><input type="text" placeholder="Filter"/></th>
                    <th><input type="text" placeholder="Filter"/></th>
                </tr>
            </thead>
            <tbody>
    """

    for ts, v, i, load, ppv, vpv, lstat, cs, err, h20, h21 in parsed:
        html += f"""
            <tr>
                <td>{ts}</td>
                <td>{v}</td>
                <td>{i}</td>
                <td>{load}</td>
                <td>{ppv}</td>
                <td>{vpv}</td>
                <td>{lstat}</td>
                <td>{cs}</td>
                <td>{err}</td>
                <td>{h20}</td>
                <td>{h21}</td>
            </tr>
        """

    html += """
            </tbody>
        </table>

        <script>
        $(document).ready(function () {
            var table = $('#explore-table').DataTable({
                pageLength: 25,
                order: [[0, 'desc']],
                initComplete: function () {
                    this.api().columns().every(function () {
                        var column = this;
                        $('input', column.header()).on('keyup change clear', function () {
                            if (column.search() !== this.value) {
                                column.search(this.value).draw();
                            }
                        });
                    });
                }
            });
        });
        </script>
    </body>
    </html>
    """

    return html

# ------------------ GET: Explore Pi Status ------------------ #
@app.route("/pi_explore")
def pi_explore():
    """
    Pi Status page — shows up to 10,000 recent entries from the pi_status table.
    """
    client = get_db()
    rs = client.execute("""
        SELECT timestamp, cpu_temp, disk, memory, uptime, wifi_signal, fan_speed, pi_name, pi_os, pi_updates
        FROM pi_status
        ORDER BY timestamp DESC
        LIMIT 10000
    """)

    html = """
    <html>
    <head>
        <title>Explore Pi Status</title>
        <link rel="stylesheet" href="https://cdn.datatables.net/1.13.6/css/jquery.dataTables.min.css">
        <script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
        <script src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.min.js"></script>
        <style>
            body { font-family: sans-serif; padding: 1em; }
            table { width: 100%; border-collapse: collapse; margin-top: 1em; }
            th, td { padding: 6px; border: 1px solid #ccc; text-align: center; }
            th { background: #f0f0f0; }
            input { width: 100%; box-sizing: border-box; }
        </style>
    </head>
    <body>
        <h2>Explore Pi Status (Latest 10,000 Entries)</h2>
        <p><a href="/">Back to Dashboard</a> | <a href="/explore">Solar Explore</a></p>
        <table id="pi-status-table" class="display">
            <thead>
                <tr>
                    <th>Time</th>
                    <th>CPU Temp</th>
                    <th>Disk Usage</th>
                    <th>Memory Usage</th>
                    <th>Uptime</th>
                    <th>Wi-Fi Signal</th>
                    <th>Fan Speed</th>
                    <th>Pi Name</th>
                    <th>Pi OS</th>
                    <th>Pi Updates</th>
                </tr>
                <tr>
                    <th><input type="text" placeholder="Filter"/></th>
                    <th><input type="text" placeholder="Filter"/></th>
                    <th><input type="text" placeholder="Filter"/></th>
                    <th><input type="text" placeholder="Filter"/></th>
                    <th><input type="text" placeholder="Filter"/></th>
                    <th><input type="text" placeholder="Filter"/></th>
                    <th><input type="text" placeholder="Filter"/></th>
                    <th><input type="text" placeholder="Filter"/></th>
                    <th><input type="text" placeholder="Filter"/></th>
                    <th><input type="text" placeholder="Filter"/></th>
                </tr>
            </thead>
            <tbody>
    """
    for row in rs.rows:
        html += f"""
            <tr>
                <td>{row[0]}</td>
                <td>{row[1]}</td>
                <td>{row[2]}</td>
                <td>{row[3]}</td>
                <td>{row[4]}</td>
                <td>{row[5]}</td>
                <td>{row[6]}</td>
                <td>{row[7]}</td>
                <td>{row[8]}</td>
                <td>{row[9]}</td>
            </tr>
        """

    html += """
            </tbody>
        </table>

        <script>
        $(document).ready(function () {
            var table = $('#pi-status-table').DataTable({
                pageLength: 25,
                order: [[0, 'desc']],
                initComplete: function () {
                    this.api().columns().every(function () {
                        var column = this;
                        $('input', column.header()).on('keyup change clear', function () {
                            if (column.search() !== this.value) {
                                column.search(this.value).draw();
                            }
                        });
                    });
                }
            });
        });
        </script>
    </body>
    </html>
    """

    return html

# -----------------------------------------------------------#
# ------------------ Main Dashboard Route ------------------ #
# -----------------------------------------------------------#

@app.route("/", methods=["GET", "HEAD"])
def index():
    """
    Main dashboard route.
    """
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
        server_log("GET", "Token decryption failed or out of range. Using default of 7 days.", "warning")

    try:
        since = now - timedelta(days=days)
        client = get_db()
        rs = client.execute(
            "SELECT timestamp, received, data FROM logs WHERE timestamp >= ? ORDER BY timestamp DESC",
            [since.isoformat()]
        )
        rows = rs.rows
    except Exception as db_err:
        server_log("GET", f"Database query failed: {db_err}", "error")
        return f"<p>Error reading database: {db_err}</p>"

    # Logger status
    if rows:
        try:
            latest_entry = json.loads(rows[0][2])
            last_ts = datetime.fromisoformat(latest_entry.get("timestamp", rows[0][0]))
        except:
            last_ts = datetime.fromisoformat(rows[0][0])
        delta = datetime.utcnow() - last_ts
        if delta.total_seconds() < 600:
            status_color, status_text = "#28a745", "Receiving data"
        else:
            status_color = "#dc3545"
            status_text = f"No data in {int(delta.total_seconds() // 60)} min"
    else:
        status_color, status_text = "#dc3545", "No data available"

    # Pi status
    pi_status_row = None
    try:
        rs_pi = client.execute(
            "SELECT ip, timestamp, uptime, cpu_temp, disk, memory, ssid, wifi_signal, fan_speed, pi_name, pi_os, pi_updates FROM pi_status ORDER BY timestamp DESC LIMIT 1"
        )
        if rs_pi.rows:
            pi_cols = ["ip", "timestamp", "uptime", "cpu_temp", "disk", "memory", "ssid", "wifi_signal", "fan_speed", "pi_name", "pi_os", "pi_updates"]
            pi_status_row = dict(zip(pi_cols, rs_pi.rows[0]))
    except Exception as e:
        server_log("GET", f"Failed to fetch Pi status: {e}", "warning")

    # Parse logs
    for row in rows:
        try:
            data = json.loads(row[2])
            ts = data.get("timestamp", row[0])
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
    if not parsed:
        latest_voltage = average_voltage = max_voltage = "N/A"
        average_load = max_load = "N/A"
        latest_vpv = 0
        vpv_message = "No data available"
        table_data = []
    else:
        parsed_chrono = []
        voltages = [p[1] for p in parsed]
        currents = [p[2] for p in parsed]
        loads = [round(v * a, 2) for v, a in zip(voltages, currents)]
        latest_voltage = f"{voltages[0]:.2f} V"
        average_voltage = f"{sum(voltages) / len(voltages):.2f} V"
        max_voltage = f"{max(voltages):.2f} V"
        average_load = f"{sum(loads) / len(loads):.2f} W"
        max_load = f"{max(loads):.2f} W"
        parsed_chrono = list(reversed(parsed))
        table_data = parsed_chrono[-20:] if len(parsed_chrono) > 20 else parsed_chrono
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

    # Voltage and current series
    rs_days = client.execute("SELECT COUNT(DISTINCT DATE(timestamp)) FROM logs")
    existing_days = rs_days.rows[0][0] if rs_days.rows else 0

    # Runtime estimates
    try:
        battery_wh = 200 * 12
        runtime_str = estimate_runtime_wh(voltages[0] * currents[0], battery_wh)
    except:
        runtime_str = "N/A"

    try:
        starlink_watt_draw = 31
        starlink_runtime_str = estimate_runtime_wh(starlink_watt_draw, battery_wh)
        h21_today = parsed[0][9]
        solar_offset_wh = (h21_today / 1000) * 5000
        net_draw = starlink_watt_draw - (solar_offset_wh / 24)
        starlink_plus_solar_runtime_str = (
            estimate_runtime_wh(net_draw, battery_wh) if net_draw > 0
            else "Infinite (solar potential exceeds draw)"
        )
    except:
        starlink_runtime_str = starlink_plus_solar_runtime_str = "N/A"

    # Charge mode aggregation
    daily_mode_totals = defaultdict(lambda: defaultdict(float))
    if len(parsed) >= 2:
        parsed_chrono = list(reversed(parsed))
        for i in range(1, len(parsed_chrono)):
            try:
                t1 = datetime.fromisoformat(parsed_chrono[i - 1][0])
                t2 = datetime.fromisoformat(parsed_chrono[i][0])
                delta = (t2 - t1).total_seconds()
                if 0 < delta < 3600:
                    mode_day = t1.date().isoformat()
                    mode = parsed_chrono[i - 1][6]
                    daily_mode_totals[mode_day][mode] += delta / 60
            except Exception as e:
                server_log("GET", f"Skipping mode delta calc due to error: {e}", "warning")

    mode_days = sorted(daily_mode_totals.keys())
    mode_types = sorted({
        mode
        for day in reversed(mode_days)
        for mode in daily_mode_totals[day]
        if not mode.startswith("Unknown")
    })

    datasets = build_mode_datasets(mode_types, daily_mode_totals, mode_days, [
        "#cce5ff", "#99ccff", "#66b2ff", "#3399ff", "#007fff", "#0066cc"
    ])

    # H20/H21 aggregation
    daily_h20 = defaultdict(float)
    daily_h21 = defaultdict(float)
    for row in reversed(parsed):
        try:
            ts = datetime.fromisoformat(row[0])
            day = ts.date().isoformat()
            daily_h20[day] = row[8]
            daily_h21[day] = max(daily_h21[day], row[9])
        except Exception as e:
            server_log("GET", f"Skipping H20/H21 aggregation due to error: {e}", "warning")

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
            (10, ("green", "recent")),
            (20, ("yellow", "moderate")),
            (float('inf'), ("red", "stale"))
        ])
        checkin_label = f"{int(checkin_age_min)} min ago"
    except:
        checkin_class, checkin_label = "gray", "Unknown"

    try:
        temp_c = float(pi_status_row['cpu_temp'].split("°C")[0].strip())
        temp_class, temp_label = make_status_pill(temp_c, [
            (50, ("green", "Cool")),
            (70, ("yellow", "Warm")),
            (float('inf'), ("red", "HOT"))
        ])
    except:
        temp_class, temp_label = "gray", "Unknown"

    try:
        dBm = int(str(pi_status_row.get("wifi_signal", "-100")).split()[0])
        wifi_class, wifi_label = make_status_pill(dBm, [
            (-80, ("red", "Poor")),
            (-70, ("orange", "Weak")),
            (-65, ("green", "Fair")),
            (-50, ("green", "Good")),
            (float('inf'), ("green", "Strong"))
        ])
    except:
        wifi_class, wifi_label = "gray", "Unknown"

    try:
        duty_value = int(pi_status_row.get("fan_speed", "0%").replace("%", "").strip())
        fan_class, fan_label = make_status_pill(duty_value, [
            (1, ("gray", "Off")),
            (50, ("green", "Low")),
            (80, ("green", "Moderate")),
            (float('inf'), ("yellow", "High"))
        ])
    except:
        fan_class, fan_label = "gray", "Unknown"

    try:
        updates_value = int(pi_status_row.get("pi_updates", "0").split()[0])
        updates_class, updates_label = make_status_pill(updates_value, [
            (1, ("green", "Up to date")),
            (float('inf'), ("red", "Updates available"))
        ])
    except:
        updates_class, updates_label = "gray", "Unknown"

    # Always set this first so it's available even if parsing fails
    is_charging = cs in ("Bulk", "Absorption")
    if 14.4 <= v <= 14.6 and is_charging: cs = "Fully Charging"

    try:
        latest_voltage_val = float(latest_voltage.replace("V", "").strip())
        if cs == "Fully Charging":
            latest_voltage_class, latest_voltage_label = "green", "Fully Charging"
        elif is_charging:
            latest_voltage_class, latest_voltage_label = "green", "Charging"
        else:
            latest_voltage_class, latest_voltage_label = make_status_pill(
                latest_voltage_val,
                [
                    (12.8, ("red", "Discharge")),
                    (13.0, ("orange", "Low")),
                    (13.28, ("yellow", "Watch")),
                    (13.5, ("green", "Good")),
                    (14.0, ("green", "Full")),
                    (float('inf'), ("green", "Charging"))
                ]
            )
    except:
        latest_voltage_class, latest_voltage_label = (
            ("green", "Charging") if is_charging else ("gray", "Unknown")
        )

    # Estimate SOC based on latest voltage
    if parsed:
        voltage_float = voltages[0]
        if is_charging:
            soc_percent, soc_color, soc_label = None, "green", "Charging"
        else:
            soc_percent = estimate_soc(voltage_float)
            soc_color, soc_label = soc_pill(soc_percent)
    else:
        soc_percent, soc_color, soc_label = 0, "gray", "Unknown"

    # Render deploy status pills
    render_class, render_label = get_render_deploy_status()

    # Build HTML 1
    html = f"""
    <html>
    <head>
        <title>VE.Direct Dashboard</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/crypto-js@4.1.1/crypto-js.min.js"></script>
        <link rel="apple-touch-icon" href="/static/icon.png">
        <link rel="icon" type="image/png" href="/static/icon.png">
        <meta name="apple-mobile-web-app-capable" content="yes">
        <meta name="apple-mobile-web-app-title" content="DeltaPi">
        <meta name="mobile-web-app-capable" content="yes">
        <meta name="theme-color" content="#1e88e5">
        <link rel="icon" type="image/x-icon" href="/static/favicon.ico">

    <style>
        body {{
            font-size: 14px;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
        }}

        input, select, button, textarea, table, th, td {{
            font-size: inherit;
            font-family: inherit;
        }}

        .pill {{
            display: inline-block;
            padding: 0.2em 0.6em;
            border-radius: 999px;
            color: #fff;
            font-weight: bold;
            font-size: 0.85em;
            line-height: 1;
        }}
        .pill.green {{ background-color: #28a745; }}
        .pill.yellow {{ background-color: #ffc107; color: #212529; }}
        .pill.red {{ background-color: #dc3545; }}
        .pill.gray {{ background-color: #6c757d; }}
    </style>
    </head>
    <body>
        <h2>VE.Direct Solar Data (Last {days} Days)</h2>
        <form method='get' style='margin-bottom: 1em;' onsubmit="event.preventDefault(); encryptAndSubmit();">
            <label for='days'>Show data for past</label>
            <input type='number' id='daysInput' value='{days}' min='1' max='60' style='width: 4em;'> days
            <input type='hidden' id='tokenInput' name='token'>
            <button type='submit'>Update</button>
        </form>

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
        </script>
    """

    # Build HTML 2
    html += f"""
    <table style="width: 100%; margin-bottom: 2em;">
        <tr>
            <td style="vertical-align: top; width: 50%; padding-right: 2em;">
                <h3>Solar System Summary</h3>
                <strong>Status:</strong> <span class="pill" style="background-color: {status_color};">{status_text}</span><br>
                <strong>Latest Voltage:</strong> {latest_voltage} <span class="pill {latest_voltage_class}">{latest_voltage_label}</span><br>
                <strong>Estimated SOC:</strong> {soc_percent}% <span class="pill" style="background-color: {soc_color};">{soc_label}</span><br>
                <strong>Max Battery Load:</strong> {max_load}<br>
                <strong>Runtime Est. Similar Load:</strong> {runtime_str}<br>
                <strong>Starlink Est. Runtime:</strong> {starlink_runtime_str}<br>
                <strong>Starlink Runtime w/Solar:</strong> {starlink_plus_solar_runtime_str}<br>
                <strong>Latest Panel Voltage (VPV):</strong> {latest_vpv:.2f} V <span class="pill" style="background-color: {vpv_color};">{vpv_message}</span><br>
                
                <hr style="margin: 1em 0; border-top: 1px solid #ccc;">
                <h3>Measured Idle Power Draws:</h3>
                Raspberry Pi Zero 2 + Fan: 2.5 W (0.19 A)<br>
                Victron MPPT 100|50 (idle): 0.4 W (0.03 A)<br>
                CO Detector: 0.8 W (0.06 A)<br>
                USB LED Indicators: 0.4 W (0.03 A)<br>
                Parasitic 12V Loads: ~0.4 W (0.03 A)<br>
                Conversion Loss Overhead (~8%): ~0.5 W (0.04 A)<br>
                <b>Total Idle Power Draw: ~5.0 W or ~0.12 kWh (0.38 A)</b><br>
                <br>
                <strong>Estimated Active Power Draws:</strong><br>
                Starlink Mini: 31 W (2.3 A)<br>
                Fridge (active): ~59 W (4.3 A)<br>

            <td style="vertical-align: top; width: 50%; border-left: 1px solid #ccc; padding-left: 2em;">
    """

    # Container disk status (ephemeral on free tier, but still useful to show)
    data_percent, data_class, data_label = get_disk_status("/tmp")

    if pi_status_row:
        html += f"""
            <h3>Pi System Health</h3>
            <strong>Pi Name: </strong><b><u>{pi_status_row['pi_name'].upper()}</u></b><br>
            <strong>Pi OS:</strong> {pi_status_row.get('pi_os')}<br>
            <strong>Uptime:</strong> {pi_status_row['uptime']}<br>
            <strong>Last Check-in:</strong> <span class="pill {checkin_class}">{checkin_label}</span><br>
            <strong>CPU Temp:</strong> {pi_status_row['cpu_temp']} <span class="pill {temp_class}">{temp_label} / {pi_status_row.get("fan_speed", "unknown")} <span class="pill {fan_class}">{fan_label}</span></span><br>
            <strong>Pi Updates:</strong> {pi_status_row.get('pi_updates')} <span class="pill {updates_class}">{updates_label}</span><br>
            <strong>Memory/Disk Usage:</strong> {pi_status_row['memory']} / {pi_status_row['disk']}<br>
            <strong>Wi-Fi SSID:</strong> {pi_status_row.get("ssid", "unknown" )} {pi_status_row.get("wifi_signal", "unknown")} <span class="pill {wifi_class}">{wifi_label}</span><br>

            <hr style="margin: 1em 0; border-top: 1px solid #ccc;">
            <h3>Container Status Summary</h3>
            <strong>Container Disk (/tmp):</strong> {data_percent}% <span class="pill {data_class}">{data_label}</span><br>
            <strong>Database:</strong> Turso (libSQL Cloud)<br>
            <strong>Days of Data Requested:</strong> {days}<br>
            <strong>Days Available in Dataset:</strong> {existing_days}<br>
            <strong>Render Deploy Status:</strong> <span class="pill {render_class}">{render_label}</span><br>
        """
    else:
        html += "<p><em>No Pi status data available.</em></p>"

    html += "</td></tr></table>"

    # Build HTML 3 - Solar Power Line Chart
    html += f"""
        <h3>Solar Power Line Chart</h3>
        <div id="chart-container" style="width: 100vw; height: 90vh; margin: 0; padding: 0;">
            <canvas id="chart" style="width: 100%; height: 100%;"></canvas>
        </div>

        <script>
        const ctx = document.getElementById('chart').getContext('2d');
        const chart = new Chart(ctx, {{
            type: 'line',
            data: {{
                labels: {json.dumps(timestamps)},
                datasets: [{{
                    label: 'Solar Power (W)',
                    data: {json.dumps(powers)},
                    borderColor: 'orange',
                    fill: false,
                    tension: 0.1
                }}]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                plugins: {{
                    legend: {{ position: 'top' }}
                }},
                scales: {{
                    x: {{
                        title: {{ display: true, text: 'Timestamp' }}
                    }},
                    y: {{
                        beginAtZero: true,
                        min: 0,
                        max: 305,
                        title: {{ display: true, text: 'Watts' }},
                        position: 'left',
                        grid: {{
                            drawOnChartArea: true,
                            drawTicks: true
                        }}
                    }},
                    y1: {{
                        beginAtZero: true,
                        min: 0,
                        max: 305,
                        title: {{ display: true, text: 'Watts' }},
                        position: 'right',
                        grid: {{
                            drawOnChartArea: false
                        }},
                        ticks: {{
                            display: true,
                            mirror: false
                        }}
                    }}
                }}
            }}
        }});
    </script>
    """

    # Build HTML 4: Battery Voltage Line Chart
    html += f"""
        <h3>Battery Voltage Line Chart</h3>
        <div id="voltage-chart-container" style="width: 100%; height: 90vh; margin: 0; padding: 0;">
            <canvas id="voltageChart" style="width: 100%; height: 100%;"></canvas>
        </div>

        <script>
        const vtx = document.getElementById('voltageChart').getContext('2d');
        const voltageChart = new Chart(vtx, {{
            type: 'line',
            data: {{
                labels: {json.dumps(voltage_timestamps)},
                datasets: [{{
                    label: 'Battery Voltage (V)',
                    data: {json.dumps(voltage_values)},
                    spanGaps: false,
                    borderColor: '#007BFF',
                    backgroundColor: 'rgba(0, 123, 255, 0.1)',
                    fill: true,
                    tension: 0.3,
                    pointRadius: 0
                }}]
            }},
            options: {{
                responsive: true,
                plugins: {{
                    legend: {{ position: 'top' }},
                    title: {{
                        display: true,
                        text: 'Battery Voltage Over Time'
                    }}
                }},
                scales: {{
                    x: {{
                        title: {{ display: true, text: 'Timestamp' }},
                        ticks: {{
                            autoSkip: true,
                            maxTicksLimit: 14.60,
                            maxRotation: 45,
                            minRotation: 45
                        }}
                    }},
                    y: {{
                        min: 12.5,
                        max: 14.60,
                        ticks: {{
                            stepSize: 0.1
                        }},
                        title: {{
                            display: true,
                            text: 'Volts'
                        }},
                        position: 'left',
                        grid: {{
                            color: (ctx) => ctx.tick.value < 12.0 ? "rgba(255,0,0,0.2)" : undefined
                        }}
                    }},
                    yRight: {{
                        min: 12.5,
                        max: 14.60,
                        ticks: {{
                            stepSize: 0.1,
                            display: true
                        }},
                        title: {{
                            display: true,
                            text: 'Volts'
                        }},
                        position: 'right',
                        grid: {{
                            drawOnChartArea: true,
                            drawTicks: true
                        }}
                    }}
                }},
            }}
        }});</script>
    """

    # HTML 5 - Daily Charge Modes Bar Chart
    html += f"""
    <h3>Daily Time in Charge Modes</h3>
    <div id="daily-mode-chart-container" style="width: 100%; height: 90vh; margin: 0; padding: 0;">
        <canvas id="dailyModeChart" style="width: 100%; height: 100%;"></canvas>
    </div>
    <script>
    const dailyCtx = document.getElementById('dailyModeChart').getContext('2d');
    const dailyModeChart = new Chart(dailyCtx, {{
        type: 'bar',
        data: {{
            labels: {json.dumps(mode_days)},
            datasets: {json.dumps(datasets)}
        }},
        options: {{
            responsive: true,
            plugins: {{
                tooltip: {{ mode: 'index', intersect: false }},
                title: {{ display: true, text: 'Time in Charge Modes (per day)' }}
            }},
            scales: {{
                x: {{
                    stacked: true,
                    title: {{ display: true, text: 'Date' }}
                }},
                y: {{
                    stacked: true,
                    beginAtZero: true,
                    max: 24,
                    ticks: {{
                        stepSize: 4,
                        callback: function(value) {{
                            return value + 'h';
                        }}
                    }},
                    title: {{
                        display: true,
                        text: 'Hours (0-24)'
                    }},
                    position: 'left',
                    grid: {{
                        drawTicks: true,
                        drawOnChartArea: true
                    }}
                }},
                yRight: {{
                    stacked: true,
                    beginAtZero: true,
                    max: 24,
                    ticks: {{
                        stepSize: 4,
                        callback: function(value) {{
                            return value + 'h';
                        }},
                        display: true
                    }},
                    title: {{
                        display: true,
                        text: 'Hours (0-24)'
                    }},
                    position: 'right',
                    grid: {{
                        drawOnChartArea: true,
                        drawTicks: true
                    }}
                }}
            }}
        }}
    }});
    </script>
    """

    # Build HTML 6: Daily Energy Production (H20) Chart
    html += f"""
    <h3>Daily Energy Production (kWh)</h3>
    <div id="daily-h20-chart-container" style="width: 100%; height: 90vh; margin: 0; padding: 0;">
        <canvas id="dailyH20Chart" style="width: 100%; height: 100%;"></canvas>
    </div>

    <script>
    const theoreticalMax = 1.5;

    const dhctx = document.getElementById('dailyH20Chart').getContext('2d');
    const dailyH20Chart = new Chart(dhctx, {{
        type: 'line',
        data: {{
            labels: {json.dumps(h20_days)},
            datasets: [{{
                label: 'Daily Solar Energy (kWh)',
                data: {json.dumps(h20_values)},
                borderColor: '#17a2b8',
                backgroundColor: 'rgba(23, 162, 184, 0.1)',
                fill: true,
                tension: 0.2,
                pointRadius: 4
            }}, {{
                label: 'Theoretical Max (1.5 kWh)',
                data: Array({len(h20_days)}).fill(theoreticalMax),
                borderColor: '#dc3545',
                borderDash: [6, 4],
                fill: false,
                pointRadius: 0,
                tension: 0
            }}, {{
                label: 'Idle Usage Threshold (~0.14 kWh)',
                data: Array({len(h20_days)}).fill(0.14),
                borderColor: '#ffc107',
                borderDash: [3, 3],
                fill: false,
                pointRadius: 0,
                tension: 0
            }}]
        }},
        options: {{
            responsive: true,
            plugins: {{
                legend: {{ position: 'top' }},
                title: {{
                    display: true,
                    text: 'Daily Solar Energy Collected'
                }}
            }},
            scales: {{
                x: {{
                    title: {{ display: true, text: 'Date' }},
                    ticks: {{ autoSkip: true, maxTicksLimit: 10 }}
                }},
                y: {{
                    title: {{ display: true, text: 'kWh' }},
                    min: 0,
                    max: 1.6,
                    position: 'left',
                    grid: {{
                        drawTicks: true,
                        drawOnChartArea: true
                    }},
                    ticks: {{
                        stepSize: 0.2
                    }}
                }},
                yRight: {{
                    title: {{ display: true, text: 'kWh' }},
                    min: 0,
                    max: 1.6,
                    position: 'right',
                    grid: {{
                        drawTicks: true,
                        drawOnChartArea: true
                    }},
                    ticks: {{
                        display: true,
                        stepSize: 0.2
                    }}
                }}
            }}
        }}
    }});
    </script>
    """

    # Build HTML 6.5: Daily Max Solar Power (H21) Chart
    html += f"""
    <h3 style="margin-top: 2.5em;">Daily Max Solar Power Output</h3>
    <div id="daily-h21-chart-container" style="width: 100%; height: 90vh; margin: 0; padding: 0;">
        <canvas id="dailyH21Chart" style="width: 100%; height: 100%;"></canvas>
    </div>

    <script>
    const dh21ctx = document.getElementById('dailyH21Chart').getContext('2d');
    const dailyH21Chart = new Chart(dh21ctx, {{
        type: 'bar',
        data: {{
            labels: {json.dumps(h21_days)},
            datasets: [{{
                label: 'Max Power (W)',
                data: {json.dumps(h21_values)},
                backgroundColor: 'rgba(40, 167, 69, 0.4)',
                borderColor: '#28a745',
                borderWidth: 1
            }}]
        }},
        options: {{
            responsive: true,
            plugins: {{
                legend: {{ display: false }},
                title: {{
                    display: true,
                    text: 'Daily Peak Solar Panel Output (Watts)'
                }},
                tooltip: {{
                    callbacks: {{
                        label: function(context) {{
                            return context.raw + ' W';
                        }}
                    }}
                }}
            }},
            scales: {{
                x: {{
                    title: {{ display: true, text: 'Date' }},
                    ticks: {{
                        autoSkip: true,
                        maxTicksLimit: 10
                    }}
                }},
                y: {{
                    title: {{ display: true, text: 'Watts' }},
                    beginAtZero: true,
                    suggestedMax: 300,
                    position: 'left',
                    grid: {{
                        drawOnChartArea: true
                    }},
                    ticks: {{
                        display: true,
                        mirror: false
                    }}
                }},
                y1: {{
                    title: {{ display: true, text: 'Watts' }},
                    beginAtZero: true,
                    suggestedMax: 300,
                    position: 'right',
                    grid: {{
                        drawOnChartArea: true
                    }},
                    ticks: {{
                        display: true,
                        mirror: false
                    }}
                }}
            }}
        }}
    }});
    </script>
    """

    # Build 7 - Navigation buttons
    html += """
    
    <script>
    const isStandalone = true;

    document.addEventListener("DOMContentLoaded", () => {
        if (isStandalone) {
            const container = document.createElement("div");
            container.style.position = "fixed";
            container.style.top = "1em";
            container.style.right = "1em";
            container.style.display = "flex";
            container.style.gap = "0.75em";
            container.style.zIndex = "999";
            document.body.appendChild(container);

            function styleBtn(el) {
                el.style.padding = "0.6em 1.2em";
                el.style.border = "none";
                el.style.borderRadius = "10px";
                el.style.backgroundColor = "#1e88e5";
                el.style.color = "#fff";
                el.style.fontSize = "16px";
                el.style.boxShadow = "0 2px 6px rgba(0,0,0,0.3)";
                el.style.cursor = "pointer";
                el.style.textDecoration = "none";
                el.style.display = "inline-block";
            }

            const exploreBtn = document.createElement("a");
            exploreBtn.textContent = "Solar Explore";
            exploreBtn.href = "/explore";
            styleBtn(exploreBtn);
            container.appendChild(exploreBtn);

            const piBtn = document.createElement("a");
            piBtn.textContent = "Pi Explore";
            piBtn.href = "/pi_explore";
            styleBtn(piBtn);
            container.appendChild(piBtn);

            const csvBtn = document.createElement("a");
            csvBtn.textContent = "CSV";
            csvBtn.href = "/export.csv?token=" + encodeURIComponent("{token}");
            csvBtn.download = "";
            styleBtn(csvBtn);
            container.appendChild(csvBtn);

            const refreshBtn = document.createElement("a");
            refreshBtn.textContent = "Refresh";
            refreshBtn.href = "#";
            refreshBtn.onclick = e => { e.preventDefault(); location.reload(); };
            styleBtn(refreshBtn);
            container.appendChild(refreshBtn);
        }
    });
    </script>
    """

    # Build 8 - Latest Readings Table
    html += """
    <h3 style="margin-top: 2em;">Latest Readings (Most Recent at Top)</h3>
    <div class="table-container" style="margin-top:1em;">
        <table border="1" cellpadding="5">
        <thead>
            <tr>
                <th>Time</th>
                <th>Battery Voltage (V)</th>
                <th>Battery Current (A)</th>
                <th>Solar Power (W)</th>
                <th>Panel Voltage (V)</th>
                <th>Load Output</th>
                <th>Charge Mode</th>
                <th>Error Code</th>
                <th>Energy Today (kWh)</th>
                <th>Max Solar Power(H21)</th>
            </tr>
        </thead>
        <tbody>
    """

    for ts, v, i, ppv, vpv, load, cs, err, h20, h21 in reversed(table_data):
        power = round(v * i, 2)
        html += f"""
                <tr>
                    <td>{ts}</td>
                    <td>{v}</td>
                    <td>{i}</td>
                    <td>{power}</td>
                    <td>{vpv}</td>
                    <td>{load}</td>
                    <td>{cs}</td>
                    <td>{err}</td>
                    <td>{h20}</td>
                    <td>{h21}</td>
                </tr>
        """

    html += """
            </tbody>
        </table>
    </div>
    </body>
    </html>
    """
    return html


if __name__ == "__main__":
    app.run()