"""
DeltaPi Camping Monitor Server (Flask App)
------------------------------------------
Flask entry point: HTTP ingestion routes (`/log`, `/log/bulk`, `/status`), the
encrypted date-range token endpoint (`/encrypt_days`), and the dashboard (`/`).

The heavy lifting lives in focused modules:
- config.py       — env-derived settings and static lookup tables
- util.py         — logging, formatters, token decryption, geo/moon helpers
- db.py           — SQLite schema, connections, retention cleanup
- integrations.py — cached external data providers (weather, AQI, alerts, …)
- energy.py       — battery/solar model (SOC, runtime, sustainability outlook)
- dashboard.py    — builds the template context from log rows + Pi status
- templates/index.html, static/style.css, static/dashboard.js — the dashboard UI

Data model (SQLite): `logs` (timestamped VE.Direct + merged battery/Starlink
frames) and `pi_status` (Raspberry Pi health reports). All ingestion routes
require HTTPS and a bearer token; the dashboard date range is Fernet-encrypted.

Hosted on Render.com with a persistent Disk at /data, paired with a field Pi
that buffers locally and bulk-uploads periodically.

Author: DeltaPI Project - Jerry Craft
"""
import hmac
import json
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, render_template
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.exceptions import HTTPException

from config import POST_SECRET, fernet, MAX_DAYS
from util import server_log, decrypt_token
from db import get_db, close_db, maybe_cleanup
from dashboard import build_context

# ------------------ App Setup ------------------ #
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)
app.config["MAX_CONTENT_LENGTH"] = 1_000_000  # reject request bodies larger than 1 MB
app.teardown_appcontext(close_db)

limiter = Limiter(key_func=get_remote_address)
limiter.init_app(app)


@app.errorhandler(413)
def request_too_large(e):
    """Return an explicit 413 (not a generic 400) when a body exceeds
    MAX_CONTENT_LENGTH, so an oversized upload is unambiguous to the client."""
    return jsonify({"error": "Payload too large",
                    "max_bytes": app.config["MAX_CONTENT_LENGTH"]}), 413


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


# ------------------ Ingestion routes ------------------ #
@app.route("/log", methods=["POST"])
@limiter.limit("3 per minute")
def log():
    """
    Accepts a single VE.Direct solar data entry via POST.
    Requires HTTPS and Bearer token auth. Validates required fields (V, I, PPV, VPV, timestamp).
    Triggers cleanup of records older than 365 days (throttled to once per 24 hours).
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
        maybe_cleanup()
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
    Triggers cleanup of records older than 365 days (throttled to once per 24 hours).
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
        maybe_cleanup()
        server_log("POST", f"Bulk insert from {client_ip}: {inserted} new entries", "info")
        return jsonify({"status": "ok", "inserted": inserted}), 200
    except Exception as e:
        server_log("POST", f"Bulk DB insert failed from {client_ip}: {e}", "error")
        return jsonify({"error": str(e)}), 500


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


# ------------------ Dashboard ------------------ #
@app.route("/", methods=["GET", "HEAD"])
def index():
    """
    Main dashboard route. Decrypts optional token to determine date range (default 7 days),
    queries solar data, delegates metric computation to dashboard.build_context, and renders
    templates/index.html. Supports light/dark theme toggle via cookie persistence.
    """
    if request.method == "HEAD":
        return "", 200

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

    ctx = build_context(conn, rows, days, now)
    return render_template("index.html", **ctx)


if __name__ == "__main__":
    app.run()
