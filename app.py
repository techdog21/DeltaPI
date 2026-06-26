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
import sys
import hmac
import time as time_module
import json
import logging
import sqlite3
import shutil
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from collections import defaultdict
from flask import Flask, request, jsonify, g
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from cryptography.fernet import Fernet
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.exceptions import HTTPException
from dateutil.parser import parse as parse_date
from flask import has_request_context
from markupsafe import escape

# ------------------ App Setup ------------------ #
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)
app.config["MAX_CONTENT_LENGTH"] = 1_000_000  # reject request bodies larger than 1 MB

@app.errorhandler(413)
def request_too_large(e):
    """Return an explicit 413 (not a generic 400) when a body exceeds
    MAX_CONTENT_LENGTH, so an oversized upload is unambiguous to the client."""
    return jsonify({"error": "Payload too large",
                    "max_bytes": app.config["MAX_CONTENT_LENGTH"]}), 413

# ------------------ Configuration ------------------ #
DB_DIR = os.environ.get("DB_DIR", "/data")
DB_PATH = os.path.join(DB_DIR, "vedirect.db")
POST_SECRET = os.environ.get("POST_SECRET")
if not POST_SECRET:
    logging.warning("POST_SECRET is not set — all authenticated routes will reject requests")
SERVER_LOG = os.path.join(DB_DIR, "server.log")
os.makedirs(DB_DIR, exist_ok=True)

FERNET_KEY = os.environ.get("FERNET_KEY")
if not FERNET_KEY:
    logging.warning("FERNET_KEY is not set — /encrypt_days will be unavailable")
fernet = Fernet(FERNET_KEY.encode()) if FERNET_KEY else None
MAX_DAYS = 30  # matches the 30-day retention enforced by cleanup_old_records
# Battery / runtime model. The MPPT reports only charge current (never house
# load), so consumption is derived from energy balance instead — see
# estimate_avg_load_w(). Constants below were calibrated against ~14 days of
# field data (parked base ~5 W; woods-with-Starlink 24h average ~45-50 W).
BATTERY_CAPACITY_WH = 200 * 12   # 200 Ah @ 12 V nominal pack (estimate fallback only)
SOC_FLOOR = 10                   # don't plan usable capacity below ~10% on LiFePO4
LOAD_WINDOW_HOURS = 72           # trailing window for the empirical load estimate
_last_cleanup = 0
_cleanup_lock = threading.Lock()
MT = ZoneInfo("America/Denver")

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
    """
    Initializes the SQLite database with required tables if they do not exist.
    Creates `logs` table for solar data and `pi_status` table for Pi health reports.
    Adds an index on logs.timestamp for efficient time-based queries.
    Called at application startup.
    """
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
                pi_updates TEXT DEFAULT 'unknown',
                controller TEXT DEFAULT 'unknown'
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs (timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pi_status_timestamp ON pi_status (timestamp)")
        # Migrate: add any missing optional columns to pi_status
        cols = {row[1] for row in conn.execute("PRAGMA table_info(pi_status)").fetchall()}
        for col in ("pi_name", "pi_os", "pi_updates", "controller"):
            if col not in cols:
                conn.execute(f"ALTER TABLE pi_status ADD COLUMN {col} TEXT DEFAULT 'unknown'")

init_db()

def get_db():
    """
    Returns a persistent SQLite connection for the current Flask request context.
    Uses sqlite3.Row for dictionary-like access. Connection is closed automatically
    by close_db() at end of request.
    """
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        g.db.row_factory = sqlite3.Row
    return g.db

def get_disk_status(path="/"):
    """
    Returns disk usage for the given path as (percent, color_class, label).
    Used in the dashboard to show container filesystem health.
    """
    total, used, _ = shutil.disk_usage(path)
    percent = int((used / total) * 100)
    if percent < 85:
        return percent, "green", "Normal"
    elif percent < 95:
        return percent, "yellow", "High"
    else:
        return percent, "red", "Critical"

def cleanup_old_records():
    """
    Deletes log records older than 30 days and pi_status records older than 7 days.
    Uses get_db() for connection management. Throttled to run once per 24 hours
    via the _last_cleanup module-level variable in the /log and /log/bulk routes.
    """
    conn = get_db()
    # Timestamps are stored as ISO-8601 (e.g. 2026-04-29T12:34:56.789012+00:00),
    # so the cutoff must use the same format for lexicographic comparison to work.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    deleted_logs = conn.execute("DELETE FROM logs WHERE timestamp < ?", (cutoff,)).rowcount
    status_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    deleted_status = conn.execute("DELETE FROM pi_status WHERE timestamp < ?", (status_cutoff,)).rowcount
    conn.commit()
    if deleted_logs or deleted_status:
        server_log("DB", f"Cleanup: removed {deleted_logs} logs (>30d) and {deleted_status} pi_status (>7d)", "info")

@app.teardown_appcontext
def close_db(exception):
    """
    Closes the SQLite connection at the end of each Flask request context.
    Prevents connections from persisting beyond their intended scope.
    """
    db = g.pop('db', None)
    if db is not None:
        db.close()

# ------------------ Helpers ------------------ #
def is_authorized():
    """
    Constant-time bearer-token check for authenticated POST routes.
    Returns False if POST_SECRET is not configured.
    """
    if not POST_SECRET:
        return False
    expected = f"Bearer {POST_SECRET}"
    provided = request.headers.get("Authorization", "")
    return hmac.compare_digest(provided, expected)

def humanize_minutes(minutes):
    """Format a minute count as 'N min', 'Hh Mm', or 'Dd Hh' (no 'ago' suffix)."""
    total = int(minutes)
    if total >= 1440:
        d, remaining = divmod(total, 1440)
        h = remaining // 60
        return f"{d}d {h}h" if h else f"{d}d"
    if total >= 60:
        h, m = divmod(total, 60)
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{total} min"

def clean_int(value):
    """
    Safely converts a value to an integer, stripping null bytes and whitespace.
    Returns 0 on any failure. Handles stray \\x00 bytes from VE.Direct data.
    """
    try:
        return int(str(value).replace("\x00", "").strip())
    except Exception:
        return 0

def fmt_runtime(draw_w, battery_wh, tag=""):
    """
    Compact runtime string from a draw (W) and usable energy (Wh):
    '2.3 d @ 40W', '18.5 h @ 130W', or '45 min @ 300W'. Returns 'idle' when the
    draw is negligible. `tag` appends a short marker (e.g. ' est').
    """
    if draw_w <= 0.5 or battery_wh <= 0:
        return "idle"
    h = battery_wh / draw_w
    span = f"{h / 24:.1f} d" if h >= 48 else (f"{h:.1f} h" if h >= 1 else f"{int(h * 60)} min")
    return f"{span} @ {draw_w:.0f}W{tag}"

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

def make_status_pill(value, thresholds):
    """
    Assigns a CSS class and label based on numeric thresholds.
    Thresholds are ordered (threshold, (class, label)) pairs.
    Returns the first match where value < threshold, or the last entry as fallback.
    """
    for threshold, (cls, label) in thresholds:
        if value < threshold:
            return cls, label
    return thresholds[-1][1]

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

def server_log(tag, message, level="info"):
    """
    Logs a timestamped message with a tag and current route to both server.log
    and Python's logging module. Falls back to 'N/A' route outside request context.
    """
    route = request.path if has_request_context() else "N/A"
    timestamp = datetime.now(timezone.utc).isoformat()
    entry = f"[{timestamp}] [{tag}] [ROUTE: {route}] {message}\n"
    try:
        with open(SERVER_LOG, "a") as f:
            f.write(entry)
    except Exception as e:
        logging.error(f"[Logger] Failed writing log file: {e}")
    getattr(logging, level)(f"[{tag}] [ROUTE: {route}] {message}")

def decrypt_token(token, min_days=1, max_days=MAX_DAYS):
    """
    Decrypts a Fernet token and returns the encoded number of days.
    Raises ValueError if token is missing, malformed, or days out of range.
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

def ensure_utc(dt):
    """Assume UTC if datetime is naive (e.g. from SQLite)."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

def fmt_mt(iso_str, fmt="%Y-%m-%d %H:%M"):
    """Convert an ISO timestamp string to Mountain Time for display."""
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MT).strftime(fmt)

# ------------------ Routes ------------------ #
@app.route("/log", methods=["POST"])
@limiter.limit("3 per minute")
def log():
    """
    Accepts a single VE.Direct solar data entry via POST.
    Requires HTTPS and Bearer token auth. Validates required fields (V, I, PPV, VPV, timestamp).
    Triggers cleanup of records older than 30 days (throttled to once per 24 hours).
    """
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if not request.is_secure:
        server_log("POST", f"Insecure request rejected from {client_ip}", "warning")
        return jsonify({"error": "HTTPS required"}), 403
    if not is_authorized():
        server_log("POST", f"Rejected: Bad Auth from {client_ip}", "warning")
        return jsonify({"error": "Unauthorized"}), 403
    entry = request.get_json()
    if not entry:
        return jsonify({"error": "No data received"}), 400
    required_fields = ["V", "I", "PPV", "VPV", "timestamp"]
    if not all(field in entry for field in required_fields):
        return jsonify({"error": "Malformed data – missing required fields"}), 400
    timestamp = entry.get("timestamp", datetime.now(timezone.utc).isoformat())
    received = datetime.now(timezone.utc).isoformat()
    data_str = json.dumps(entry)
    try:
        conn = get_db()
        conn.execute("INSERT INTO logs (timestamp, received, data) VALUES (?, ?, ?)", (timestamp, received, data_str))
        conn.commit()
        global _last_cleanup
        with _cleanup_lock:
            if time_module.time() - _last_cleanup > 86400:
                cleanup_old_records()
                _last_cleanup = time_module.time()
        server_log("POST", f"Accepted data from {client_ip}", "info")
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        server_log("POST", f"DB error while logging from {client_ip}: {e}", "error")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/log/bulk", methods=["POST"])
@limiter.limit("5 per minute")
def bulk_log():
    """
    Accepts a bulk POST of VE.Direct solar data entries as a JSON list.
    Deduplicates by timestamp against existing records before inserting.
    Requires HTTPS and Bearer token auth. Primary data ingestion route used by the Pi.
    Triggers cleanup of records older than 30 days (throttled to once per 24 hours).
    """
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if not request.is_secure:
        server_log("POST", f"Insecure bulk request from {client_ip}", "warning")
        return jsonify({"error": "HTTPS required"}), 403
    if not is_authorized():
        server_log("POST", f"Rejected bulk request: Bad Auth from {client_ip}", "warning")
        return jsonify({"error": "Unauthorized"}), 403
    try:
        entries = request.get_json()
        if not isinstance(entries, list):
            raise ValueError("Expected a list of entries")
    except HTTPException:
        raise  # e.g. 413 Payload Too Large — let Flask return the right status
    except Exception as e:
        server_log("POST", f"Bad bulk JSON from {client_ip}: {e}", "warning")
        return jsonify({"error": "Invalid JSON"}), 400
    try:
        conn = get_db()
        timestamps_to_check = [e.get("timestamp") for e in entries if "timestamp" in e]
        if timestamps_to_check:
            placeholders = ",".join("?" for _ in timestamps_to_check)
            existing_ts = set(row[0] for row in conn.execute(
                "SELECT timestamp FROM logs WHERE timestamp IN ({})".format(placeholders),
                timestamps_to_check
            ))
        else:
            existing_ts = set()
        inserted = 0
        for entry in entries:
            ts = entry.get("timestamp")
            if not ts or ts in existing_ts:
                continue
            received = datetime.now(timezone.utc).isoformat()
            data_str = json.dumps(entry)
            conn.execute("INSERT INTO logs (timestamp, received, data) VALUES (?, ?, ?)", (ts, received, data_str))
            inserted += 1
        conn.commit()
        global _last_cleanup
        with _cleanup_lock:
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
    """
    Encrypts the requested number of days (1 to MAX_DAYS) into a Fernet token for secure URL usage.
    Used by the dashboard's date range selector to prevent tampering with query parameters.
    """
    try:
        if not fernet:
            server_log("GET", "FERNET_KEY not configured", "error")
            return jsonify({"error": "Encryption unavailable"}), 503
        if not request.is_secure:
            server_log("GET", f"Insecure request rejected from {request.remote_addr}", "warning")
            return jsonify({"error": "HTTPS required"}), 403
        raw_days = request.args.get("days", "7")
        days = int(raw_days)
        if not (1 <= days <= MAX_DAYS):
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
    """
    Accepts a POST with Raspberry Pi system health stats (uptime, cpu_temp, disk,
    memory, ssid, wifi_signal, and optional fan_speed, pi_name, pi_os, pi_updates).
    Requires HTTPS and Bearer token auth. Stores in pi_status table.
    """
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if not request.is_secure:
        server_log("POST", f"Insecure status update rejected from {client_ip}", "warning")
        return jsonify({"error": "HTTPS required"}), 403
    if not is_authorized():
        server_log("POST", f"Rejected: Bad Auth for /status from {client_ip}", "warning")
        return jsonify({"error": "Unauthorized"}), 403
    try:
        payload = request.get_json()
        required_fields = ["uptime", "cpu_temp", "disk", "memory", "ssid", "wifi_signal"]
        if not all(k in payload for k in required_fields):
            raise ValueError("Missing required status fields")
        conn = get_db()
        conn.execute(
            """INSERT INTO pi_status (ip, timestamp, uptime, cpu_temp, disk, memory, ssid, wifi_signal, fan_speed, pi_name, pi_os, pi_updates, controller)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (client_ip, datetime.now(timezone.utc).isoformat(), payload["uptime"], payload["cpu_temp"],
             payload["disk"], payload["memory"], payload["ssid"], payload["wifi_signal"],
             payload.get("fan_speed", "unknown"), payload.get("pi_name", "unknown"),
             payload.get("pi_os", "unknown"), payload.get("pi_updates", "unknown"),
             payload.get("controller", "unknown"))
        )
        conn.commit()
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        server_log("POST", f"Status update failed from {client_ip}: {e}", "warning")
        return jsonify({"error": "Invalid payload"}), 400

# ------------------ Dashboard ------------------ #
@app.route("/", methods=["GET", "HEAD"])
def index():
    """
    Main dashboard route. Decrypts optional token to determine date range (default 7 days),
    queries solar and Pi status data, computes metrics (voltage, SOC, runtime estimates),
    and renders a single-page dashboard with four Chart.js charts and a readings table.
    Supports light/dark theme toggle via cookie persistence.
    """
    if request.method == "HEAD":
        return "", 200

    parsed = []
    now = datetime.now(timezone.utc)
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
            "SELECT ip, timestamp, uptime, cpu_temp, disk, memory, ssid, wifi_signal, fan_speed, pi_name, pi_os, pi_updates, controller FROM pi_status ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        if row:
            pi_status_row = dict(zip(
                ["ip", "timestamp", "uptime", "cpu_temp", "disk", "memory", "ssid", "wifi_signal", "fan_speed", "pi_name", "pi_os", "pi_updates", "controller"], row
            ))
    except Exception as e:
        server_log("GET", f"Failed to fetch Pi status: {e}", "warning")

    # Parse logs
    batt_series = []  # (ts, soc%, house_load_w) for frames carrying measured battery data
    for row in rows:
        try:
            data = json.loads(row["data"])
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
                batt_series.append((ts, bat["soc"], house))
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

    # Voltage chart
    voltage_timestamps, voltage_values = build_voltage_series(parsed_chrono)
    timestamps = [fmt_mt(p[0]) for p in reversed(parsed)]
    powers = [p[3] for p in reversed(parsed)]

    # Battery charts (measured) — chronological; only frames carrying battery data.
    batt_chrono = list(reversed(batt_series))
    batt_times = [fmt_mt(b[0]) for b in batt_chrono]
    batt_soc_values = [b[1] for b in batt_chrono]
    batt_load_values = [b[2] for b in batt_chrono]
    SOC_DANGER = 20  # red floor line: regularly draining below this shortens LiFePO4 life
    # Daily-energy chart: scale to the data instead of a fixed 1.6 ceiling
    h20_ymax = max([round(max(h20_values) * 1.25, 1), 0.5]) if h20_values else 1.6

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

    controller_val = pi_status_row.get("controller", "unknown")
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
        house_load_str = f"{house_load_w:.0f} W"
        runtime_str = fmt_runtime(house_load_w, usable_wh)
    else:
        avg_load_w = estimate_avg_load_w(conn, now) if parsed else None
        usable_wh = BATTERY_CAPACITY_WH * max(0, soc_percent - SOC_FLOOR) / 100 if parsed else 0
        house_load_str = f"~{avg_load_w:.0f} W est" if avg_load_w else "—"
        runtime_str = fmt_runtime(avg_load_w, usable_wh, " est") if avg_load_w else "N/A"

    # Current solar power (latest panel watts) for the Solar System panel
    solar_now = parsed[0][3] if parsed else 0
    # Solar has no "bad" state, so color only conveys producing (green) vs off
    # (gray); the label carries the magnitude.
    solar_class, solar_label = make_status_pill(solar_now, [
        (5, ("gray", "Off")), (50, ("green", "Low")),
        (150, ("green", "Good")), (float('inf'), ("green", "Strong")),
    ])

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
        :root, [data-theme="dark"] {{
            --bg: #0a0a0a;
            --bg-panel: #111;
            --bg-input: #1a1a1a;
            --border: #1e1e1e;
            --border-light: #222;
            --text: #e0e0e0;
            --text-muted: #888;
            --text-dim: #666;
            --text-table: #bbb;
            --accent: #4fc3f7;
            --accent-hover: #81d4fa;
            --grid-color: #1a1a1a;
            --row-border: #1a1a1a;
            --pill-green: #2e7d32;
            --pill-yellow: #f9a825;
            --pill-yellow-text: #1a1a1a;
            --pill-red: #c62828;
            --pill-gray: #424242;
            --pill-orange: #e65100;
            --chart-voltage: #4fc3f7;
            --chart-voltage-fill: rgba(79,195,247,0.08);
            --chart-power: #ff9800;
            --chart-h20: #26a69a;
            --chart-h20-fill: rgba(38,166,154,0.08);
            --chart-h21: rgba(102,187,106,0.5);
            --chart-h21-border: #66bb6a;
        }}
        [data-theme="light"] {{
            --bg: #f4f5f7;
            --bg-panel: #ffffff;
            --bg-input: #f0f0f0;
            --border: #ddd;
            --border-light: #ccc;
            --text: #1a1a1a;
            --text-muted: #666;
            --text-dim: #999;
            --text-table: #333;
            --accent: #0277bd;
            --accent-hover: #01579b;
            --grid-color: #e8e8e8;
            --row-border: #eee;
            --pill-green: #2e7d32;
            --pill-yellow: #f9a825;
            --pill-yellow-text: #1a1a1a;
            --pill-red: #c62828;
            --pill-gray: #9e9e9e;
            --pill-orange: #e65100;
            --chart-voltage: #0277bd;
            --chart-voltage-fill: rgba(2,119,189,0.1);
            --chart-power: #e65100;
            --chart-h20: #00897b;
            --chart-h20-fill: rgba(0,137,123,0.1);
            --chart-h21: rgba(56,142,60,0.4);
            --chart-h21-border: #388e3c;
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'IBM Plex Sans', sans-serif;
            font-size: 13px;
            background: var(--bg);
            color: var(--text);
            min-height: 100vh;
            overflow-x: hidden;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
        }}
        .header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 8px 16px;
            background: var(--bg-panel);
            border-bottom: 1px solid var(--border-light);
            flex-shrink: 0;
        }}
        .header h1 {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 15px;
            font-weight: 700;
            color: var(--accent);
            letter-spacing: 1px;
        }}
        .header-controls {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}
        .header form {{
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .header input[type="number"] {{
            width: 48px;
            background: var(--bg-input);
            border: 1px solid var(--border-light);
            color: var(--text);
            padding: 3px 6px;
            border-radius: 4px;
            font-family: 'JetBrains Mono', monospace;
            font-size: 12px;
        }}
        .header button, .theme-toggle {{
            background: var(--accent);
            color: var(--bg);
            border: none;
            padding: 4px 12px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
            cursor: pointer;
            font-family: 'IBM Plex Sans', sans-serif;
        }}
        .header button:hover, .theme-toggle:hover {{ background: var(--accent-hover); }}
        .header label {{ font-size: 11px; color: var(--text-muted); }}

        .dashboard {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            grid-auto-rows: min-content;
            gap: 6px;
            padding: 6px;
            flex: 1;
        }}
        .panel-wide {{ grid-column: 1 / -1; }}

        .panel {{
            background: var(--bg-panel);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 10px 12px;
            overflow: hidden;
        }}
        .panel h2 {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 11px;
            font-weight: 600;
            color: var(--accent);
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
        .metric-label {{ color: var(--text-muted); }}
        .metric-value {{ color: var(--text); font-family: 'JetBrains Mono', monospace; font-weight: 600; font-size: 12px; }}

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
        .pill.green {{ background: var(--pill-green); }}
        .pill.yellow {{ background: var(--pill-yellow); color: var(--pill-yellow-text); }}
        .pill.red {{ background: var(--pill-red); }}
        .pill.gray {{ background: var(--pill-gray); }}
        .pill.orange {{ background: var(--pill-orange); }}

        .chart-panel {{
            background: var(--bg-panel);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 6px 8px;
            display: flex;
            flex-direction: column;
            height: 240px;
        }}
        .chart-panel h2 {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 10px;
            font-weight: 600;
            color: var(--accent);
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
            background: var(--bg-panel);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 8px 12px;
            overflow-x: auto;
            flex-shrink: 0;
        }}
        .table-panel h2 {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 10px;
            font-weight: 600;
            color: var(--accent);
            text-transform: uppercase;
            letter-spacing: 1.5px;
            margin-bottom: 4px;
        }}
        table {{ width: 100%; border-collapse: collapse; }}
        th {{
            font-size: 10px;
            font-weight: 600;
            color: var(--text-dim);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            padding: 3px 6px;
            text-align: center;
            border-bottom: 1px solid var(--border-light);
        }}
        td {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 11px;
            padding: 3px 6px;
            text-align: center;
            color: var(--text-table);
            border-bottom: 1px solid var(--row-border);
        }}

        @media (max-width: 768px) {{
            html, body {{ overflow-x: hidden; max-width: 100%; }}
            body {{ overflow-y: auto; height: auto; }}
            .dashboard {{
                grid-template-columns: 1fr;
                grid-template-rows: auto;
                overflow-x: hidden;
                overflow-y: auto;
            }}
            .panel-wide {{ grid-column: 1; }}
            .table-panel {{ grid-column: 1; }}
            .chart-panel {{ min-height: 200px; }}
            .header {{ flex-wrap: wrap; gap: 6px; }}
            .header h1 {{ font-size: 13px; }}
            /* compact controls so they fit on one line (wrap only as a safety) */
            .header-controls {{ flex-wrap: wrap; gap: 4px; width: 100%; }}
            .header form {{ gap: 4px; }}
            .header button, .theme-toggle {{ padding: 3px 6px; font-size: 10px; }}
            .header input[type="number"] {{ width: 38px; padding: 2px 4px; font-size: 10px; }}
            .header label {{ font-size: 10px; }}
            #autoRefresh {{ padding: 3px 4px !important; font-size: 10px; }}
            /* let long values (battery feed, wifi) wrap instead of forcing width */
            .metric {{ font-size: 11px; flex-wrap: wrap; }}
            .metric-value {{ font-size: 11px; word-break: break-word; text-align: right; }}
        }}
    </style>
    <script>
    // Theme: load from cookie or default to dark
    (function() {{
        var match = document.cookie.match(/theme=(dark|light)/);
        var theme = match ? match[1] : 'dark';
        document.documentElement.setAttribute('data-theme', theme);
    }})();
    </script>
</head>
<body>

<div class="header">
    <h1>DELTAPI SOLAR MONITOR</h1>
    <div class="header-controls">
        <form method="get" onsubmit="event.preventDefault(); encryptAndSubmit();">
            <label>Last</label>
            <input type="number" id="daysInput" value="{days}" min="1" max="{MAX_DAYS}">
            <label>days</label>
            <input type="hidden" id="tokenInput" name="token">
            <button type="submit">Update</button>
        </form>
        <button class="theme-toggle" onclick="toggleTheme()">Theme</button>
        <button class="theme-toggle" onclick="location.reload()">Refresh</button>
        <select id="autoRefresh" class="theme-toggle" onchange="setAutoRefresh(this.value)" style="padding:4px 6px;">
            <option value="0">Auto: Off</option>
            <option value="15">15s</option>
            <option value="30">30s</option>
            <option value="60">1m</option>
            <option value="120">2m</option>
            <option value="300">5m</option>
        </select>
    </div>
</div>

<div class="dashboard">
    <!-- Battery Array (listed first: the priority view on a phone at camp) -->
    <div class="panel">
        <h2>Battery Array</h2>
        <div class="metric"><span class="metric-label">SOC</span><span class="metric-value">{soc_percent}% <span class="pill {soc_color}">{soc_label}</span></span></div>
        <div class="metric"><span class="metric-label">Battery V</span><span class="metric-value">{latest_voltage} <span class="pill {latest_voltage_class}">{latest_voltage_label}</span></span></div>
        <div class="metric"><span class="metric-label">Battery feed</span><span class="metric-value">{batt_per_display}<span class="pill {batt_feed_class}">{batt_feed_label}</span></span></div>
        <div class="metric"><span class="metric-label">Consumption</span><span class="metric-value">{house_load_str}</span></div>
        <div class="metric"><span class="metric-label">Runtime</span><span class="metric-value">{runtime_str}</span></div>
    </div>

    <!-- Solar System -->
    <div class="panel">
        <h2>Solar System</h2>
        <div class="metric"><span class="metric-label">Solar data</span><span class="metric-value"><span class="pill {status_color}">{status_text}</span></span></div>
        <div class="metric"><span class="metric-label">Solar power</span><span class="metric-value">{solar_now} W <span class="pill {solar_class}">{solar_label}</span></span></div>
        <div class="metric"><span class="metric-label">Panel V</span><span class="metric-value">{latest_vpv:.2f} V <span class="pill {vpv_color}">{vpv_message}</span></span></div>
    </div>

    <!-- Pi Health (full width below the two summary panels) -->
    <div class="panel panel-wide">"""

    if pi_status_row:
        # Escape every Pi-reported field; the Pi controls these strings, so a
        # crafted /status payload could otherwise inject HTML/JS into the page.
        pi_name = escape((pi_status_row.get('pi_name') or '?').upper())
        pi_os_val = escape(pi_status_row.get('pi_os') or '?')
        pi_uptime = escape(pi_status_row.get('uptime') or '?')
        pi_cpu_temp = escape(pi_status_row.get('cpu_temp') or '?')
        pi_fan_speed = escape(pi_status_row.get('fan_speed') or '?')
        pi_updates_val = escape(pi_status_row.get('pi_updates') or '?')
        pi_memory = escape(pi_status_row.get('memory') or '?')
        pi_disk = escape(pi_status_row.get('disk') or '?')
        pi_ssid = escape(pi_status_row.get('ssid') or '?')
        pi_wifi_signal = escape(pi_status_row.get('wifi_signal') or '?')
        html += f"""
        <h2>Pi Health — {pi_name}</h2>
        <div class="metric"><span class="metric-label">Controller</span><span class="metric-value"><span class="pill {controller_class}">{controller_label}</span></span></div>
        <div class="metric"><span class="metric-label">OS</span><span class="metric-value">{pi_os_val}</span></div>
        <div class="metric"><span class="metric-label">Uptime</span><span class="metric-value">{pi_uptime}</span></div>
        <div class="metric"><span class="metric-label">Last Check-in</span><span class="metric-value"><span class="pill {checkin_class}">{checkin_label}</span></span></div>
        <div class="metric"><span class="metric-label">CPU / Fan</span><span class="metric-value">{pi_cpu_temp} <span class="pill {temp_class}">{temp_label}</span> {pi_fan_speed} <span class="pill {fan_class}">{fan_label}</span></span></div>
        <div class="metric"><span class="metric-label">Updates</span><span class="metric-value">{pi_updates_val} <span class="pill {updates_class}">{updates_label}</span></span></div>
        <div class="metric"><span class="metric-label">Mem / Disk</span><span class="metric-value">{pi_memory} / {pi_disk}</span></div>
        <div class="metric"><span class="metric-label">Wi-Fi</span><span class="metric-value">{pi_ssid} {pi_wifi_signal} <span class="pill {wifi_class}">{wifi_label}</span></span></div>
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

    <!-- Chart: Battery SOC (measured) -->
    <div class="chart-panel">
        <h2>Battery SOC (%)</h2>
        <div class="chart-wrap"><canvas id="chartSOC"></canvas></div>
    </div>

    <!-- Chart: Consumption (measured) -->
    <div class="chart-panel">
        <h2>Consumption (W)</h2>
        <div class="chart-wrap"><canvas id="chartLoad"></canvas></div>
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
        # load/cs originate from VE.Direct payloads (user-controlled via /log) — escape.
        html += f"""
                <tr><td>{fmt_mt(ts)}</td><td>{v}</td><td>{i}</td><td>{power}</td>
                <td>{vpv}</td><td>{escape(load)}</td><td>{escape(cs)}</td><td>{h20}</td><td>{h21}</td></tr>"""

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

function toggleTheme() {{
    var current = document.documentElement.getAttribute('data-theme') || 'dark';
    var next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    document.cookie = 'theme=' + next + ';path=/;max-age=31536000';
    location.reload();
}}

var _refreshTimer = null;
function setAutoRefresh(seconds) {{
    if (_refreshTimer) clearInterval(_refreshTimer);
    _refreshTimer = null;
    localStorage.setItem('autoRefresh', seconds);
    if (seconds > 0) {{
        _refreshTimer = setInterval(function() {{ location.reload(); }}, seconds * 1000);
    }}
}}
(function() {{
    var saved = localStorage.getItem('autoRefresh') || '0';
    var sel = document.getElementById('autoRefresh');
    if (sel) sel.value = saved;
    if (parseInt(saved) > 0) setAutoRefresh(parseInt(saved));
}})();

// Read CSS variable values for chart colors
var style = getComputedStyle(document.documentElement);
var gridColor = style.getPropertyValue('--grid-color').trim();
var textMuted = style.getPropertyValue('--text-muted').trim();

const chartOpts = (yMin, yMax, stepSize) => ({{
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    plugins: {{ legend: {{ display: false }} }},
    elements: {{ point: {{ radius: 0 }}, line: {{ borderWidth: 1.5 }} }},
    scales: {{
        x: {{ display: false }},
        y: {{ min: yMin, max: yMax, ticks: {{ stepSize: stepSize, font: {{ size: 9 }}, color: textMuted }}, grid: {{ color: gridColor }} }}
    }}
}});

// Solar Power
new Chart(document.getElementById('chartPower'), {{
    type: 'line',
    data: {{
        labels: {json.dumps(timestamps)},
        datasets: [{{ data: {json.dumps(powers)}, borderColor: style.getPropertyValue('--chart-power').trim(), fill: false, tension: 0.1 }}]
    }},
    options: chartOpts(0, 305, 50)
}});

// Battery Voltage
new Chart(document.getElementById('chartVoltage'), {{
    type: 'line',
    data: {{
        labels: {json.dumps(voltage_timestamps)},
        datasets: [{{ data: {json.dumps(voltage_values)}, borderColor: style.getPropertyValue('--chart-voltage').trim(), backgroundColor: style.getPropertyValue('--chart-voltage-fill').trim(), fill: true, tension: 0.3, spanGaps: false }}]
    }},
    options: chartOpts(12.5, 14.6, 0.5)
}});

// Daily Energy H20 (solar production; scaled to data)
new Chart(document.getElementById('chartH20'), {{
    type: 'line',
    data: {{
        labels: {json.dumps(h20_days)},
        datasets: [
            {{ data: {json.dumps(h20_values)}, borderColor: style.getPropertyValue('--chart-h20').trim(), backgroundColor: style.getPropertyValue('--chart-h20-fill').trim(), fill: true, tension: 0.2, pointRadius: 2 }}
        ]
    }},
    options: chartOpts(0, {h20_ymax}, {round(h20_ymax / 4, 2)})
}});

// Battery SOC (measured) with a red danger floor at {SOC_DANGER}%
new Chart(document.getElementById('chartSOC'), {{
    type: 'line',
    data: {{
        labels: {json.dumps(batt_times)},
        datasets: [
            {{ data: {json.dumps(batt_soc_values)}, borderColor: style.getPropertyValue('--chart-voltage').trim(), backgroundColor: style.getPropertyValue('--chart-voltage-fill').trim(), fill: true, tension: 0.3, pointRadius: 0 }},
            {{ data: Array({len(batt_times)}).fill({SOC_DANGER}), borderColor: style.getPropertyValue('--pill-red').trim(), borderDash: [4,3], fill: false, pointRadius: 0 }}
        ]
    }},
    options: chartOpts(0, 100, 20)
}});

// House Load (measured, W)
new Chart(document.getElementById('chartLoad'), {{
    type: 'line',
    data: {{
        labels: {json.dumps(batt_times)},
        datasets: [{{ data: {json.dumps(batt_load_values)}, borderColor: style.getPropertyValue('--chart-power').trim(), fill: false, tension: 0.2, pointRadius: 0 }}]
    }},
    options: chartOpts(0, null, null)
}});
</script>
</body>
</html>"""

    return html

if __name__ == "__main__":
    app.run()