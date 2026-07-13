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

from config import DB_PATH, SEED_LOCATIONS
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
                cpu_load TEXT DEFAULT 'unknown',
                backup_count TEXT DEFAULT 'unknown',
                backup_latest TEXT DEFAULT 'unknown'
            )
        """)
        # Persistent tier of the external-API cache (see integrations.py):
        # one row per (provider, rounded location), overwritten on refresh, so
        # weather & friends survive deploys/restarts instead of a cold re-pull.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ext_cache (
                provider TEXT NOT NULL,
                loc_key TEXT NOT NULL,
                epoch REAL NOT NULL,
                data TEXT NOT NULL,
                PRIMARY KEY (provider, loc_key)
            )
        """)
        # Saved weather locations (header dropdown) + a tiny key/value settings
        # store for the active selection. Lets the user pin a spot for the
        # weather lookup when the dish (e.g. Starlink Mini) won't share GPS.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # On a fresh DB, seed the saved locations and select the first one, so
        # weather has a manual location to fall back on out of the box.
        if not conn.execute("SELECT 1 FROM locations LIMIT 1").fetchone() and SEED_LOCATIONS:
            for loc in SEED_LOCATIONS:
                conn.execute("INSERT INTO locations (name, lat, lon) VALUES (?, ?, ?)",
                             (loc["name"], loc["lat"], loc["lon"]))
            first_id = conn.execute("SELECT id FROM locations ORDER BY id LIMIT 1").fetchone()[0]
            if not conn.execute("SELECT 1 FROM app_settings WHERE key = 'active_location'").fetchone():
                conn.execute("INSERT INTO app_settings (key, value) VALUES ('active_location', ?)",
                             (str(first_id),))
        # One-time correction: the first Grayback Gulch seed shipped with Melba's
        # coordinates by mistake (the real spot is up past Idaho City in the Boise
        # NF). Fix any DB still carrying the wrong point; a user-edited row won't
        # match these exact values, so it's left alone. Safe to remove later.
        conn.execute(
            "UPDATE locations SET lat = ?, lon = ? "
            "WHERE name = 'Grayback Gulch' AND lat = 43.4451 AND lon = -116.5296",
            (43.80673, -115.868826))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs (timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pi_status_timestamp ON pi_status (timestamp)")
        # Migrate: add any missing optional columns to pi_status
        cols = {row[1] for row in conn.execute("PRAGMA table_info(pi_status)").fetchall()}
        for col in ("pi_name", "pi_os", "pi_updates", "controller", "cpu_load", "backup_count", "backup_latest"):
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


# ------------------ Saved locations / settings ------------------ #
def list_locations(conn):
    """All saved weather locations (id, name, lat, lon), alphabetical."""
    return conn.execute(
        "SELECT id, name, lat, lon FROM locations ORDER BY name COLLATE NOCASE"
    ).fetchall()


def add_location(conn, name, lat, lon):
    """Insert a saved location and return its new id (commits)."""
    cur = conn.execute("INSERT INTO locations (name, lat, lon) VALUES (?, ?, ?)",
                       (name, lat, lon))
    conn.commit()
    return cur.lastrowid


def get_setting(conn, key, default=None):
    """Read a value from the key/value app_settings store."""
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(conn, key, value):
    """Upsert a value into the key/value app_settings store (commits)."""
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)))
    conn.commit()


def get_active_location(conn):
    """The manually-pinned weather location as {'id','name','lat','lon'}, or None
    when the selection is Auto (follow dish GPS / home fallback) or missing."""
    sel = get_setting(conn, "active_location")
    if not sel or sel == "auto":
        return None
    row = conn.execute("SELECT id, name, lat, lon FROM locations WHERE id = ?",
                       (sel,)).fetchone()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "lat": row[2], "lon": row[3]}


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
    # Drop ext_cache rows for locations nobody has viewed in a week (a roaming
    # rig leaves one row per provider per rounded location behind).
    conn.execute("DELETE FROM ext_cache WHERE epoch < ?",
                 (time_module.time() - 7 * 86400,))
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
