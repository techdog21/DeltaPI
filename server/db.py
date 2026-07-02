"""
DeltaPi — SQLite storage layer.

Schema init, per-request connection management, disk-usage reporting, and the
throttled retention cleanup. Imported once at startup (init_db runs on import).
"""
import sqlite3
import shutil
import threading
import time as time_module
from datetime import datetime, timedelta, timezone
from flask import g

from config import DB_PATH
from util import server_log


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
                controller TEXT DEFAULT 'unknown',
                cpu_load TEXT DEFAULT 'unknown'
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs (timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pi_status_timestamp ON pi_status (timestamp)")
        # Migrate: add any missing optional columns to pi_status
        cols = {row[1] for row in conn.execute("PRAGMA table_info(pi_status)").fetchall()}
        for col in ("pi_name", "pi_os", "pi_updates", "controller", "cpu_load"):
            if col not in cols:
                conn.execute(f"ALTER TABLE pi_status ADD COLUMN {col} TEXT DEFAULT 'unknown'")


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


def close_db(exception):
    """
    Closes the SQLite connection at the end of each Flask request context.
    Prevents connections from persisting beyond their intended scope.
    Registered on the app via app.teardown_appcontext.
    """
    db = g.pop('db', None)
    if db is not None:
        db.close()


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
    Deletes log records older than 365 days and pi_status records older than 7 days.
    Uses get_db() for connection management. Throttled to run once per 24 hours
    via maybe_cleanup().
    """
    conn = get_db()
    # Timestamps are stored as ISO-8601 (e.g. 2026-04-29T12:34:56.789012+00:00),
    # so the cutoff must use the same format for lexicographic comparison to work.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    deleted_logs = conn.execute("DELETE FROM logs WHERE timestamp < ?", (cutoff,)).rowcount
    status_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    deleted_status = conn.execute("DELETE FROM pi_status WHERE timestamp < ?", (status_cutoff,)).rowcount
    conn.commit()
    if deleted_logs or deleted_status:
        server_log("DB", f"Cleanup: removed {deleted_logs} logs (>365d) and {deleted_status} pi_status (>7d)", "info")


_last_cleanup = 0
_cleanup_lock = threading.Lock()


def maybe_cleanup():
    """Run cleanup_old_records at most once per 24 h; called from the ingest routes."""
    global _last_cleanup
    with _cleanup_lock:
        if time_module.time() - _last_cleanup > 86400:
            cleanup_old_records()
            _last_cleanup = time_module.time()


init_db()
