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
- Provides encrypted token-based access to dashboards, exports, and data
- Offers a secure dashboard at `/` with time-series charts, system stats, and runtime estimates
- Supports CSV export (`/export.csv`), Pi health reports (`/status`), and exploratory views (`/explore`, `/pi_explore`)
- Implements HTTPS enforcement and token-based authorization
- Includes Flask-Limiter for rate limiting and abuse prevention
- Uses Fernet encryption for secure token generation
- Automatic cleanup of records older than 30 days (throttled to once per 24 hours)
- Render deploy status monitoring via Render API
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
- `/export.csv`    Exports solar data to CSV (token required)
- `/encrypt_days`  Encrypts number of days for secure token use
- `/debug`         Shows latest 10 raw solar entries (token required)
- `/explore`       Client-filterable solar data table (up to 10k rows)
- `/pi_explore`    Client-filterable Pi health data table

Dashboard Metrics:
------------------
- Latest/Average/Max battery voltage with status pills
- Estimated SOC (LiFePO4 voltage curve, resting only)
- Max/Average battery load
- Runtime estimates (current load, Starlink, Starlink + solar offset)
- Panel voltage with sunlight condition indicator
- Daily energy production (H20) with theoretical max and idle threshold lines
- Daily max solar power output (H21)
- Daily time in charge modes (stacked bar)
- Battery voltage over time (line chart)
- Solar power over time (line chart)
- Pi system health: CPU temp, Wi-Fi signal, fan speed, disk, memory, uptime, OS updates
- Container status: disk usage, data retention days, Render deploy age

Security:
---------
- All data ingestion and export routes require HTTPS and a valid bearer token
- Tokens used in dashboards and exports are encrypted via Fernet to prevent tampering
- Rate limiting on all POST and sensitive GET endpoints

Intended Use:
-------------
This app is intended to be hosted on Render.com with persistent storage at
/var/data/vedirect and paired with a field-deployed Raspberry Pi sending VE.Direct
and system health data. It supports offline buffering and visualization to aid in
remote solar monitoring and diagnostics.
"""

# ------------------ Imports ------------------ #
import os # For file and directory handling
import time as time_module # For time-based operations
import json # For JSON handling
import logging # For logging
import sqlite3 # For SQLite database handling
import shutil # For file operations
from datetime import datetime, timedelta, timezone # For date/time handling
from collections import defaultdict # For date/time handling and data aggregation
from flask import Flask, request, jsonify # For Flask 2.0+ compatibility
from flask_limiter import Limiter # For rate limiting
from flask_limiter.util import get_remote_address # For rate limiting
from cryptography.fernet import Fernet # For secure token encryption
from werkzeug.middleware.proxy_fix import ProxyFix # For Flask 2.0+ compatibility
from flask import g # For global request context
from dateutil.parser import parse as parse_date # For flexible date parsing
from flask import has_request_context, request  # Add these to imports

# ------------------ App Setup ------------------ #
# Create Flask app and apply ProxyFix middleware
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)

# ------------------ Configuration ------------------ #
# DB_DIR = "/var/data/vedirect" # Directory for SQLite database and logs
# DB_PATH = os.path.join(DB_DIR, "vedirect.db") # Path to SQLite database file
DB_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DB_DIR, "vedirect.db")
POST_SECRET = os.environ.get("POST_SECRET") # Secret for POST requests
# SERVER_LOG = os.path.join(DB_DIR, "server.log") # Path to server log file
SERVER_LOG = os.path.join(DB_DIR, "server.log")
os.makedirs(DB_DIR, exist_ok=True) # Ensure the database directory exists

# Fernet key for encrypting tokens
FERNET_KEY = os.environ.get("FERNET_KEY")
fernet = Fernet(FERNET_KEY.encode()) if FERNET_KEY else None
MAX_DAYS = 60 # Maximum days for data queries
_last_cleanup = 0 # Timestamp of last cleanup operation
# ------------------ Rate Limiting ------------------ #
# Initialize Flask-Limiter with a key function to get the remote address
limiter = Limiter(key_func=get_remote_address)
limiter.init_app(app)

# ------------------ Charge State Mapping ------------------ #
# Maps charge state codes to human-readable strings
CS_MAP = {
    "0": "Off",
    "1": "Low Power",
    "2": "Fault",
    "3": "Bulk",
    "4": "Absorption",
    "5": "Float"
}

# ------------------ DB Init ------------------ #
# Initializes the SQLite database with required tables if they do not exist.
def init_db():
    """
    Initializes the SQLite database with tables for:
    
    1. **Solar Data Logs (`logs`)**
       - Stores timestamped VE.Direct data entries from the solar controller.
       - Includes:
         - `id`: Auto-incrementing primary key.
         - `timestamp`: Timestamp of the solar event from the device.
         - `received`: UTC time when the server received the entry.
         - `data`: Raw JSON string containing voltage, current, charge state, etc.
       - Indexed by `timestamp` for efficient time-based queries.

    2. **Pi System Status (`pi_status`)**
       - Stores periodic Raspberry Pi health/status updates.
       - Each record represents a snapshot of system health at a given time.
       - Includes:
         - `id`: Auto-incrementing primary key.
         - `ip`: Sender's IP address (helps if multiple Pis report in).
         - `timestamp`: UTC time the status was received.
         - `uptime`: Human-readable uptime string (e.g., "2 days, 3:21").
         - `cpu_temp`: CPU temperature (e.g., "44.1°C").
         - `disk`: Disk usage summary (e.g., "3.1G/29G (10%)").
         - `memory`: RAM usage summary (e.g., "420M/925M (45%)").

    This function is called at application startup to ensure required tables exist.
    """
    with sqlite3.connect(DB_PATH) as conn:
        # Logs Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                received TEXT,
                data TEXT
            )
        """)

        # Pi Status Table (for system health dashboard)
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

        # Index on timestamp for faster queries
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs (timestamp)")


# Call the init_db function to ensure the database is set up
init_db()

def get_db():
    """
    Retrieves a persistent SQLite database connection for the current Flask request context.
    
    - If a connection doesn't exist yet in `g`, it creates and stores one.
    - Uses `sqlite3.Row` to allow dictionary-like access to query results.

    Returns:
        sqlite3.Connection: The database connection to use for this request.
    """
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

def get_disk_status(path="/"):
    """
    Get disk usage for the given path.
    Returns: (usage_percent, color, label)
    """
    total, used, _ = shutil.disk_usage(path)
    percent = int((used / total) * 100)

    if percent < 70:
        return percent, "green", "Normal"
    elif percent < 90:
        return percent, "yellow", "High"
    else:
        return percent, "red", "Critical"

def cleanup_old_records():
    """Delete all records older than 30 days from the logs table."""
    cutoff = datetime.utcnow() - timedelta(days=30)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    deleted = conn.execute("DELETE FROM logs WHERE timestamp < ?", (cutoff_str,)).rowcount
    conn.commit()
    if deleted:
        server_log("DB", f"Cleanup: removed {deleted} records older than 30 days", "info")

@app.teardown_appcontext
def close_db(exception):
    """
    Closes the database connection at the end of each Flask application context (i.e., after each request).

    This prevents database connections from persisting beyond their intended scope and ensures clean teardown.
    
    Parameters:
        exception (Optional[Exception]): Exception, if any, that occurred during the request.
    """
    db = g.pop('db', None)
    if db is not None:
        db.close()

# ------------------ Helper: Mode Datasets ------------------ #
def build_mode_datasets(modes:list, daily_mode_totals:dict, days:list, blue_shades:list) -> list:
    '''
    Constructs Chart.js-compatible datasets for a stacked bar chart of solar charge modes.

    Parameters:
    - modes (list of str): Unique charge modes (e.g., 'Bulk', 'Float', etc.).
    - daily_mode_totals (dict): Nested dictionary where daily_mode_totals[day][mode] = minutes.
    - days (list of str): Ordered list of ISO date strings representing each day.
    - blue_shades (list of str): List of hex color codes to shade each mode.

    Returns:
    - list of dicts: Each dict contains the mode label, time-in-hours data, and a background color.
    '''
    datasets = []
    for idx, mode in enumerate(modes):
        data = [
            round(daily_mode_totals[day].get(mode, 0) / 60, 1)  # minutes → hours
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

    Parameters:
        value (any): The input value, expected to be a string or number.

    Returns:
        int: The cleaned integer value, or 0 if conversion fails.

    Notes:
        - Handles stray null bytes (\x00) that may appear in VE.Direct data.
        - Returns 0 on any exception (invalid input, empty string, etc.).
    """
    try:
        return int(str(value).replace("\x00", "").strip())
    except:
        return 0
    
# ------------------ Helper: Runtime Estimation ------------------ #
def estimate_runtime_wh(draw_w, battery_wh):
    """
    Estimates how long a battery can run at the current power draw.

    Parameters:
        draw_w (float): Current power draw in watts (W).
        battery_wh (float): Total battery capacity in watt-hours (Wh).

    Returns:
        str: Human-readable runtime estimate (e.g., "12.5 hours (~0.5 days)"),
             or "Idle or charging" if the draw is too low to calculate.
    
    Notes:
        - Ignores battery efficiency losses or discharge curves.
        - Threshold of 0.5 W is used to filter out idle or negligible draw.
    """
    if draw_w > 0.5:
        hours = battery_wh / draw_w
        return f"{hours:.1f} hours (~{hours/24:.1f} days)"
    return "Idle or charging"

# ------------------ Helper: Status Pill ------------------ #
def make_status_pill(value, thresholds):
    """
    Assigns a status class and label based on numeric thresholds.

    Parameters:
        value (float): The value to evaluate (e.g., temperature, signal strength).
        thresholds (list of tuples): Ordered list of (threshold, (class, label)) pairs.
            - Each threshold defines the upper bound for a range.
            - The class is a CSS class (e.g., 'green', 'red') for styling.
            - The label is a human-readable status description.

    Returns:
        tuple: (class, label) corresponding to the first threshold the value is below.
               If none match, returns the class/label from the last threshold.
    
    Example:
        thresholds = [
            (50, ('green', 'Cool')),
            (70, ('yellow', 'Warm')),
            (float('inf'), ('red', 'Hot'))
        ]

        make_status_pill(66, thresholds) → ('yellow', 'Warm')
    """
    for threshold, (cls, label) in thresholds:
        if value < threshold:
            return cls, label
    return thresholds[-1][1]

# ------------------ Helper: Voltage Series ------------------ #
def build_voltage_series(parsed):
    """
    Extracts and formats a voltage time series from parsed solar data.

    Parameters:
        parsed (list of tuples): Each tuple is expected to contain at least:
            - ts (str): ISO 8601 timestamp string.
            - v (float): Voltage reading.
            - *_: Any additional values (ignored).

    Returns:
        tuple:
            - timestamps (list of str): Formatted timestamps as "YYYY-MM-DD HH:MM".
            - values (list of float or None): Corresponding voltages (rounded to 2 decimals),
              or None if voltage is below 11 V (e.g., noise, invalid readings).

    Notes:
        - Skips entries with invalid timestamps.
        - Useful for generating battery voltage line charts.
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
    Logs a message with a timestamp, a custom tag, and the current request route (if available).

    Parameters:
        tag (str): A short identifier for the log entry (e.g., "POST", "GET", "DB").
        message (str): The log message content.
        level (str): Logging level as a string (e.g., "info", "warning", "error").
                     Defaults to "info". Must match a method in the `logging` module.

    Behavior:
        - Writes the log entry to both the console (via Python's `logging` module)
          and to the local `server.log` file.
        - Includes the UTC timestamp and current Flask route if in a request context.
        - Falls back to "N/A" if called outside a request context (e.g., during startup).

    Example Log Format:
        [2025-07-27T18:00:00Z] [POST] [ROUTE: /log] Accepted data from 192.168.1.2
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

    Parameters:
        token (str): Encrypted Fernet token (URL-safe base64).
        min_days (int): Minimum allowed days (inclusive).
        max_days (int): Maximum allowed days (inclusive).

    Returns:
        int: Number of days if valid.

    Raises:
        ValueError: If token is missing, malformed, or out of range.
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

def estimate_soc(voltage):
    """
    Estimates SOC for LiFePO₄ batteries based on resting voltage.
    Assumes no heavy load or active charging.
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


# ------------------ Main Dashboard Route ------------------ #
@app.route("/log", methods=["POST"])
@limiter.limit("3 per minute")
def log():
    '''
    Accepts a single VE.Direct solar data entry via POST and stores it in the database.

    Security:
    - Requires HTTPS.
    - Requires a valid Bearer token in the Authorization header.

    Payload:
    - Expects a JSON object with fields like V, I, PPV, VPV, timestamp, etc.

    Returns:
    - 200 OK on success.
    - 403 if HTTPS is missing or authorization fails.
    - 400 if data is malformed or incomplete.
    - 500 if database insertion fails.
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
        conn = get_db()
        conn.execute(
            "INSERT INTO logs (timestamp, received, data) VALUES (?, ?, ?)",
            (timestamp, received, data_str)
        )
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

# ------------------ Bulk Log Route ------------------ #    
@app.route("/log/bulk", methods=["POST"])
@limiter.limit("2 per minute")  # Slightly slower to reduce accidental flooding
def bulk_log():
    '''
    Accepts a bulk POST of VE.Direct solar data entries, validates, and inserts them into the database.

    - Expects Authorization header: Bearer POST_SECRET
    - Expects JSON body as a list of entry dicts, each with a "timestamp" key

    For each entry:
    - Skips duplicates based on existing timestamps in the database
    - Inserts only new entries with a UTC timestamp and full JSON payload

    Returns:
    - 200 OK with count of inserted entries if successful
    - 403 if unauthorized
    - 400 if JSON is invalid
    - 500 on any other error
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
        conn = get_db()
        timestamps_to_check = [e.get("timestamp") for e in entries if "timestamp" in e]
        placeholders = ",".join("?" for _ in timestamps_to_check)

        if timestamps_to_check:
            existing_ts = set(
                row[0] for row in conn.execute(
                    f"SELECT timestamp FROM logs WHERE timestamp IN ({placeholders})",
                    timestamps_to_check
                )
            )
        else:
            existing_ts = set()

        inserted = 0
        for entry in entries:
            ts = entry.get("timestamp")
            if not ts or ts in existing_ts:
                continue

            received = datetime.utcnow().isoformat()
            data_str = json.dumps(entry)

            conn.execute(
                "INSERT INTO logs (timestamp, received, data) VALUES (?, ?, ?)",
                (ts, received, data_str)
            )
            inserted += 1
        conn.commit()  # Commit all changes to the database

        server_log("POST", f"Bulk insert from {client_ip}: {inserted} new entries", "info")
        return jsonify({"status": "ok", "inserted": inserted}), 200
    except Exception as e:
        server_log("POST", f"Bulk DB insert failed from {client_ip}: {e}", "error")
        return jsonify({"error": str(e)}), 500

# ------------------ Encrypt Days Route ------------------ #
@app.route("/encrypt_days")
@limiter.limit("10 per minute")
def encrypt_days():
    """
    Encrypt the number of days requested by the user for secure URL usage.

    This endpoint is used to generate a secure token that encodes the number of 
    days of solar data the user wants to view. The token is used in URLs to 
    prevent tampering.

    Request Parameters:
        - days (int, 1-60): Number of days to encrypt.

    Returns:
        - 200 OK: JSON object with the encrypted token.
        - 400 Bad Request: If input is invalid or encryption fails.
        - 403 Forbidden: If HTTPS is not used.
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

# ------------------ CSV Export Route ------------------ #    
@app.route("/export.csv")
@limiter.limit("5 per minute")
def export_csv():
    """
    Export the last N days of VE.Direct data in CSV format.
    Requires a valid encrypted 'token' query parameter specifying the number of days (1–30).
    Responds with a downloadable CSV file containing timestamped voltage, current, power,
    and other key metrics.
    """

    try:
        if not request.is_secure:
            server_log("POST", f"CSV export blocked: Insecure request from {request.remote_addr}", "warning")
            return "HTTPS required", 403

        token = request.args.get("token")
        days = decrypt_token(token)  # Decrypt the token to get the number of days
    except Exception as e:
        server_log("POST", f"CSV export failed: Invalid or missing token from {request.remote_addr} - {e}", "error")
        days = 7  # fallback to 7 days

    since = datetime.utcnow() - timedelta(days=days)
    output = "timestamp,voltage,current,ppv,vpv,load,charge_mode,error,h20\n"

    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT timestamp, data FROM logs WHERE timestamp >= ? ORDER BY timestamp DESC",
            (since.isoformat(),)
        ).fetchall()

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

# ------------------ Debug Route ------------------ #
@app.route("/debug")
def debug():
    """
    Render a simple debug page showing the 10 most recent entries in the logs table.
    
    Security:
        - Requires a valid encrypted 'token' query parameter to authorize access.
        - Intended for admin or developer use only.
    
    Returns:
        - 403 if token is missing, invalid, or out of range.
        - 500 if database access fails.
        - 200 HTML page with recent log entries on success.
    """
    token = request.args.get("token")
    if not token:
        server_log("GET", f"/debug access denied from {request.remote_addr}: missing token", "warning")
        return "<p>Unauthorized: token missing.</p>", 403

    try:
        days = decrypt_token(token, min_days=1)  # Decrypt the token to get the number of days
    except Exception as e:
        server_log("GET", f"/debug access denied from {request.remote_addr}: {e}", "warning")
        return "<p>Unauthorized or invalid token.</p>", 403

    try:
        conn = get_db()
        rows = conn.execute("SELECT timestamp, data FROM logs ORDER BY timestamp DESC LIMIT 10").fetchall()
    except Exception as db_err:
        server_log("GET", f"/debug DB read error from {request.remote_addr}: {db_err}", "error")
        return "<p>Error reading database.</p>", 500

    server_log("GET", f"/debug accessed successfully from {request.remote_addr}", "info")

    html = "<html><head><title>Debug Logs</title></head><body>"
    html += "<h2>Latest Entries (Most Recent First)</h2><ul style='font-family: monospace;'>"

    for ts, data in rows:
        escaped_data = str(data).replace("<", "&lt;").replace(">", "&gt;")  # Simple HTML escaping
        html += f"<li><pre>{ts} - {escaped_data}</pre></li>"

    html += "</ul><p><a href='/'>⬅️ Back to Dashboard</a></p></body></html>"
    return html

# ------------------ Pi Status Route ------------------ #
@app.route("/status", methods=["POST"])
@limiter.limit("2 per minute")
def pi_status():
    """
    Accepts a POST request from a Raspberry Pi containing system health statistics.

    Expected JSON Payload:
    {
        "uptime": "2 days, 3:21",
        "cpu_temp": "44.0°C / 111.2°F",
        "disk": "3.1G/29G (10%)",
        "memory": "420M/925M (45%)",
        "ssid": "MyWiFi",
        "wifi_signal": "-66 dBm",
        "fan_speed": "40%",
        "pi_name": "delta-zero",
        "pi_os": "Raspbian GNU/Linux 11 (bullseye)",
        "pi_updates": "3 available updates"
    }

    Security:
    - Requires HTTPS
    - Requires Authorization header: Bearer POST_SECRET

    Returns:
    - 200 OK on success
    - 403 if HTTPS or authorization fails
    - 400 if required data is missing or malformed
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

        # Optional fields with fallback
        fan_speed = payload.get("fan_speed", "unknown")
        pi_name = payload.get("pi_name", "unknown")
        pi_os = payload.get("pi_os", "unknown")
        pi_updates = payload.get("pi_updates", "unknown")

        conn = get_db()
        conn.execute(
            """INSERT INTO pi_status 
            (ip, timestamp, uptime, cpu_temp, disk, memory, ssid, wifi_signal, fan_speed, pi_name, pi_os, pi_updates)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
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
            )
        )
        conn.commit()
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        server_log("POST", f"Status update failed from {client_ip}: {e}", "warning")
        return jsonify({"error": "Invalid payload"}), 400


# ------------------ Explore Route ------------------ #
@app.route("/explore")
def explore():
    """
    Explore Data page — shows up to 5000 recent log entries with client-side filtering,
    including H21 (Max Solar Power Today).
    """
    conn = get_db()
    rows = conn.execute("SELECT timestamp, received, data FROM logs ORDER BY timestamp DESC LIMIT 10000").fetchall()

    parsed = []
    for row in rows:
        try:
            data = json.loads(row["data"])
            v = clean_int(data.get("V", 0)) / 1000
            i = clean_int(data.get("I", 0)) / 1000
            ppv = clean_int(data.get("PPV", 0))
            vpv = clean_int(data.get("VPV", 0)) / 1000
            load = data.get("LOAD", "N/A")
            cs = CS_MAP.get(str(data.get("CS", "0")), f"Unknown ({data.get('CS')})")
            err = data.get("ERR", "0")
            h20 = clean_int(data.get("H20", 0)) / 100
            h21 = clean_int(data.get("H21", 0))

            ts = data.get("timestamp", row["timestamp"])
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
        <h2>🔍 Explore Solar Data (Latest 10,000 Entries)</h2>
        <p><a href="/">⬅️ Back to Dashboard</a></p>
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
                    <th><input type="text" placeholder="🔍"/></th>
                    <th><input type="text" placeholder="🔍"/></th>
                    <th><input type="text" placeholder="🔍"/></th>
                    <th><input type="text" placeholder="🔍"/></th>
                    <th><input type="text" placeholder="🔍"/></th>
                    <th><input type="text" placeholder="🔍"/></th>
                    <th><input type="text" placeholder="🔍"/></th>
                    <th><input type="text" placeholder="🔍"/></th>
                    <th><input type="text" placeholder="🔍"/></th>
                    <th><input type="text" placeholder="🔍"/></th>
                    <th><input type="text" placeholder="🔍"/></th>
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

# ------------------ Pi Status Route ------------------ #
@app.route("/pi_explore")
def pi_explore():
    """
    Pi Status page — shows up to 10,000 recent entries from the pi_status table with client-side filtering.
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT timestamp, cpu_temp, disk, memory, uptime, wifi_signal, fan_speed, pi_name, pi_os, pi_updates
        FROM pi_status
        ORDER BY timestamp DESC
        LIMIT 10000
    """).fetchall()

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
        <h2>🔍 Explore Pi Status (Latest 10,000 Entries)</h2>
        <p><a href="/">⬅️ Back to Dashboard</a> | <a href="/explore">🌞 Explore Solar Data</a></p>
        <table id="pi-status-table" class="display">
            <thead>
                <tr>
                    <th>Time</th>
                    <th>CPU Temp (°C)</th>
                    <th>Disk Usage (%)</th>
                    <th>Memory Usage (%)</th>
                    <th>Uptime</th>
                    <th>Wi-Fi Signal (dBm)</th>
                    <th>Fan Speed (%)</th>
                    <th>Pi Name</th>
                    <th>Pi OS</th>
                    <th>Pi Updates</th>
                </tr>
                <tr>
                    <th><input type="text" placeholder="🔍"/></th>
                    <th><input type="text" placeholder="🔍"/></th>
                    <th><input type="text" placeholder="🔍"/></th>
                    <th><input type="text" placeholder="🔍"/></th>
                    <th><input type="text" placeholder="🔍"/></th>
                    <th><input type="text" placeholder="🔍"/></th>
                    <th><input type="text" placeholder="🔍"/></th>
                    <th><input type="text" placeholder="🔍"/></th>
                    <th><input type="text" placeholder="🔍"/></th>
                    <th><input type="text" placeholder="🔍"/></th>
                </tr>
            </thead>
            <tbody>
    """
    for row in rows:
        html += f"""
            <tr>
                <td>{row['timestamp']}</td>
                <td>{row['cpu_temp']}</td>
                <td>{row['disk']}</td>
                <td>{row['memory']}</td>
                <td>{row['uptime']}</td>
                <td>{row['wifi_signal']}</td>
                <td>{row['fan_speed']}</td>
                <td>{row['pi_name']}</td>
                <td>{row['pi_os']}</td>
                <td>{row['pi_updates']}</td>
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
    Decrypts optional token to determine date range, retrieves matching data from the database,
    and begins assembling the dashboard.
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
        table_data = parsed_chrono[-20:] if len(parsed_chrono) > 20 else parsed_chrono
        latest_vpv = parsed[0][4]
        vpv_message = (
            "Nighttime" if latest_vpv < 5 else
            "Good Sunlight" if 16 <= latest_vpv <= 45 else
            "Over-Voltage" if latest_vpv > 45 else
            "Cloudy"
        )
    vpv_color = (
        "gray" if latest_vpv < 5 else           # gray
        "green" if 16 <= latest_vpv <= 45 else   # green
        "red" if latest_vpv > 45 else          # red
        "amber"                                  # amber for cloudy
    )

    # Voltage and current series
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
            else "Infinite (solar potential exceeds draw)"
        )
    except Exception:
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
    except Exception:
        checkin_class, checkin_label = "gray", "Unknown"

    try:
        temp_c = float(pi_status_row['cpu_temp'].split("°C")[0].strip())
        temp_class, temp_label = make_status_pill(temp_c, [
            (50, ("green", "Cool")),
            (70, ("yellow", "Warm")),
            (float('inf'), ("red", "HOT"))
        ])
    except Exception:
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
    except Exception:
        wifi_class, wifi_label = "gray", "Unknown"

    try:
        duty_value = int(pi_status_row.get("fan_speed", "0%").replace("%", "").strip())
        fan_class, fan_label = make_status_pill(duty_value, [
            (1, ("gray", "Off")),
            (50, ("green", "Low")),
            (80, ("green", "Moderate")),
            (float('inf'), ("yellow", "High"))
        ])
    except Exception:
        fan_class, fan_label = "gray", "Unknown"

    try:
        updates_value = int(pi_status_row.get("pi_updates", "0").split()[0])
        updates_class, updates_label = make_status_pill(updates_value, [
            (1, ("green", "Up to date")),
            (float('inf'), ("red", "Updates available"))
        ])
    except Exception:
        updates_class, updates_label = "gray", "Unknown"

    # Determine charging state from latest reading
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
    except Exception:
            latest_voltage_class, latest_voltage_label = (
                ("green", "Charging") if is_charging else ("gray", "Unknown")
            )
    
    # Estimate SOC based on latest voltage
    if parsed:
        voltage_float = voltages[0]  # Keep your original latest reading index
        if is_charging:
            soc_percent, soc_color, soc_label = None, "green", "Charging"
        else:
            soc_percent = estimate_soc(voltage_float)
            soc_color, soc_label = soc_pill(soc_percent)
    else:
        soc_percent, soc_color, soc_label = 0, "gray", "Unknown"

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
        <!-- Days selection form -->
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
            <!-- Solar Summary -->
            <td style="vertical-align: top; width: 50%; padding-right: 2em;">
                <h3>🔆 Solar System Summary</h3>
                <strong>Status:</strong> <span class="pill" style="background-color: {status_color};">{status_text}</span><br>
                <strong>Latest Voltage:</strong> {latest_voltage} <span class="pill {latest_voltage_class}">{latest_voltage_label}</span><br>
                <strong>Average Voltage:</strong> {average_voltage}<br>
                <strong>Max Voltage:</strong> {max_voltage}<br>
                <strong>Estimated SOC:</strong> {soc_percent}% <span class="pill" style="background-color: {soc_color};">{soc_label}</span><br>
                <strong>Max Battery Load:</strong> {max_load}<br>
                <strong>Average Battery Load:</strong> {average_load}<br>
                <strong>Runtime Est. Similar Load:</strong> {runtime_str}<br>
                <strong>Starlink Est. Runtime:</strong> {starlink_runtime_str}<br>
                <strong>Starlink Runtime w/Solar:</strong> {starlink_plus_solar_runtime_str}<br>
                <strong>Latest Panel Voltage (VPV):</strong> {latest_vpv:.2f} V <span class="pill" style="background-color: {vpv_color};">{vpv_message}</span><br>
                
                <hr style="margin: 1em 0; border-top: 1px solid #ccc;">
                <h3>Measured Idle Power Draws:</h3>
                • Raspberry Pi Zero 2 + Fan: 2.5 W (0.19 A)<br>
                • Victron MPPT 100|50 (idle): 0.4 W (0.03 A)<br>
                • CO Detector: 0.8 W (0.06 A)<br>
                • USB LED Indicators: 0.4 W (0.03 A)<br>
                • Parasitic 12V Loads: ~0.4 W (0.03 A)<br>
                • Conversion Loss Overhead (~8%): ~0.5 W (0.04 A)<br>
                <b>Total Idle Power Draw: ~5.0 W or ~0.12 kWh (0.38 A)</b><br>
                <br>
                <strong>Estimated Active Power Draws:</strong><br>
                • Starlink Mini: 31 W (2.3 A)<br>
                • Fridge (active): ~59 W (4.3 A)<br>

            <!-- Pi Status -->
            <td style="vertical-align: top; width: 50%; border-left: 1px solid #ccc; padding-left: 2em;">
    """

    # Get server disk usage for root and vedirect
    data_percent, data_class, data_label = get_disk_status(DB_DIR)

    if pi_status_row:
        html += f"""
            <h3>🤖 Pi System Health</h3>
            <strong>Pi Name: </strong><b><u>{pi_status_row['pi_name'].upper()}</u></b><br>
            <strong>Pi OS:</strong> {pi_status_row.get('pi_os')}<br>
            <strong>Uptime:</strong> {pi_status_row['uptime']}<br>
            <strong>Last Check-in:</strong> <span class="pill {checkin_class}">{checkin_label}</span><br>
            <strong>CPU Temp:</strong> {pi_status_row['cpu_temp']} <span class="pill {temp_class}">{temp_label} / {pi_status_row.get("fan_speed", "unknown")} <span class="pill {fan_class}">{fan_label}</span></span><br>
            <strong>Pi Updates:</strong> {pi_status_row.get('pi_updates')} <span class="pill {updates_class}">{updates_label}</span><br>
            <strong>Memory/Disk Usage:</strong> {pi_status_row['memory']} / {pi_status_row['disk']}<br>
            <strong>Wi-Fi SSID:</strong> {pi_status_row.get("ssid", "unknown" )} {pi_status_row.get("wifi_signal", "unknown")} <span class="pill {wifi_class}">{wifi_label}</span><br>

            <hr style="margin: 1em 0; border-top: 1px solid #ccc;">
            <h3>🐳 Container Status Summary</h3>
            <strong>Data Volume ({DB_DIR}):</strong> {data_percent}% <span class="pill {data_class}">{data_label}</span><br>
            <strong>Days of Data Requested:</strong> {days}<br>
            <strong>Days Available in Dataset:</strong> {existing_days}<br>
        """
    else:
        html += "<p><em>No Pi status data available.</em></p>"

    html += "</td></tr></table>"

    # Build HTML 3
    html += f"""
        <!-- Solar Power Line Chart -->
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
                maintainAspectRatio: false,  // ← critical line
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
        <!-- Battery Voltage Line Chart -->
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
    # Build HTML 6: Daily Max Energy (H20) Chart
    html += f"""
    <!-- Daily Energy Production (kWh) -->
    <h3>Daily Energy Production (kWh)</h3>
    <div id="daily-h20-chart-container" style="width: 100%; height: 90vh; margin: 0; padding: 0;">
        <canvas id="dailyH20Chart" style="width: 100%; height: 100%;"></canvas>
    </div>

    <script>
    const theoreticalMax = 1.5;  // 300W x 5h = 1.5 kWh

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
    <!-- Daily Max Solar Power (W) -->
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
    # Build 7 - Latest Readings Table
    html += """
    
    <script>
    const isStandalone = true;

    document.addEventListener("DOMContentLoaded", () => {
        if (isStandalone) {
            // Container for buttons
            const container = document.createElement("div");
            container.style.position = "fixed";
            container.style.top = "1em";
            container.style.right = "1em";
            container.style.display = "flex";
            container.style.gap = "0.75em";
            container.style.zIndex = "999";
            document.body.appendChild(container);

            // Shared button style
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

            // 🔍 Explore (Solar)
            const exploreBtn = document.createElement("a");
            exploreBtn.textContent = "🌞 Solar Explore";
            exploreBtn.href = "/explore";
            styleBtn(exploreBtn);
            container.appendChild(exploreBtn);

            // 🤖 Pi Explore (New)
            const piBtn = document.createElement("a");
            piBtn.textContent = "🤖 Pi Explore";
            piBtn.href = "/pi_explore";
            styleBtn(piBtn);
            container.appendChild(piBtn);

            // 📄 CSV
            const csvBtn = document.createElement("a");
            csvBtn.textContent = "📄 CSV";
            csvBtn.href = "/export.csv?token=" + encodeURIComponent("{token}");
            csvBtn.download = "";
            styleBtn(csvBtn);
            container.appendChild(csvBtn);

            // 🔄 Refresh
            const refreshBtn = document.createElement("a");
            refreshBtn.textContent = "🔄 Refresh";
            refreshBtn.href = "#";
            refreshBtn.onclick = e => { e.preventDefault(); location.reload(); };
            styleBtn(refreshBtn);
            container.appendChild(refreshBtn);
        }
    });
    </script>
    """

    # --- Build 8 - Latest Readings Table ---
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
    """

    # --- Final closing tags ---
    html += """
    </body>
    </html>
    """
    return html



if __name__ == "__main__":
    app.run()
