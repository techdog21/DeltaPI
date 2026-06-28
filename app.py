"""
DeltaPi Camping Monitor Server (Flask App)
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
import math
import logging
import sqlite3
import shutil
import threading
import requests
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
MAX_DAYS = 365  # matches the 365-day retention enforced by cleanup_old_records
# Battery / runtime model. The MPPT reports only charge current (never house
# load), so consumption is derived from energy balance instead — see
# estimate_avg_load_w(). Constants below were calibrated against ~14 days of
# field data (parked base ~5 W; woods-with-Starlink 24h average ~45-50 W).
BATTERY_CAPACITY_WH = 200 * 12   # 200 Ah @ 12 V nominal pack (estimate fallback only)
SOC_FLOOR = 10                   # don't plan usable capacity below ~10% on LiFePO4
LOAD_WINDOW_HOURS = 72           # trailing window for the empirical load estimate

def _env_float(name):
    try:
        return float(os.environ[name])
    except Exception:
        return None
# Fallback weather location (set in the Render dashboard) used when the connected
# dish is the home dish (no GPS over the API). Kept out of the repo for privacy.
HOME_LAT = _env_float("HOME_LAT")
HOME_LON = _env_float("HOME_LON")
HOME_DISH_ID = os.environ.get("HOME_DISH_ID")  # round home dish id -> use HOME_LAT/LON
FIRMS_MAP_KEY = os.environ.get("FIRMS_MAP_KEY")  # NASA FIRMS wildfire detections (free signup)
SOLAR_KWP = _env_float("SOLAR_KWP")        # array peak kW for Forecast.Solar (e.g. 0.4 = 400 W)
SOLAR_TILT = _env_float("SOLAR_TILT")      # panel tilt deg from horizontal (default 0 = flat roof)
SOLAR_AZIMUTH = _env_float("SOLAR_AZIMUTH")  # panel facing, Forecast.Solar convention (0 = south)
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

# VE.Direct charger error codes (ERR) -> short labels; unmapped codes show "Error N"
ERR_MAP = {
    "0": "OK", "2": "Battery high V", "17": "Overtemp", "18": "Over-current",
    "19": "Current reversed", "20": "Bulk time exceeded", "21": "Sensor issue",
    "26": "Terminals hot", "28": "Power stage", "33": "PV over-voltage",
    "34": "PV over-current", "38": "PV shutdown (batt V)", "65": "Comm lost",
    "67": "BMS lost", "116": "Cal lost", "117": "Bad firmware", "119": "Settings lost",
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
    Deletes log records older than 365 days and pi_status records older than 7 days.
    Uses get_db() for connection management. Throttled to run once per 24 hours
    via the _last_cleanup module-level variable in the /log and /log/bulk routes.
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

def _avg_complete_days(values, n=7):
    """Average of recent COMPLETE days, dropping the most recent (still-partial)
    day, over up to n days. Same unit in/out. None when history is too thin."""
    if not values or len(values) < 2:
        return None
    vals = values[:-1][-n:]          # drop today's partial value, keep last n complete days
    return sum(vals) / len(vals) if vals else None

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

# WMO weather codes -> short labels (Open-Meteo current/daily weather_code)
WMO_CODES = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog", 51: "Lt drizzle", 53: "Drizzle", 55: "Hvy drizzle",
    61: "Lt rain", 63: "Rain", 65: "Hvy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Lt snow", 73: "Snow", 75: "Hvy snow", 77: "Snow", 80: "Showers", 81: "Showers",
    82: "Hvy showers", 85: "Snow showers", 86: "Snow showers", 95: "Thunderstorm",
    96: "Thunderstorm", 99: "Thunderstorm",
}
_weather_cache = {}  # (lat, lon) -> (epoch, data)

def get_weather(lat, lon):
    """Current + 2-day forecast from Open-Meteo (free, no API key), cached ~20 min.
    Returns the parsed dict or None (no location / fetch failure)."""
    if lat is None or lon is None:
        return None
    key = (round(lat, 2), round(lon, 2))
    cached = _weather_cache.get(key)
    if cached and time_module.time() - cached[0] < 1200:
        return cached[1]
    try:
        resp = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,dew_point_2m,cloud_cover,weather_code,wind_speed_10m,wind_gusts_10m,precipitation,cape",
            "minutely_15": "precipitation",
            "daily": "weather_code,temperature_2m_min,shortwave_radiation_sum,sunrise,sunset",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "timezone": "auto", "forecast_days": 2,
        }, timeout=8)
        data = resp.json()
        _weather_cache[key] = (time_module.time(), data)
        return data
    except Exception as e:
        server_log("GET", f"weather fetch failed: {e}", "warning")
        return cached[1] if cached else None

_solar_fc_cache = {}  # (lat, lon) -> (epoch, dict)

def get_solar_forecast(lat, lon):
    """Predicted PV production (kWh) for today and tomorrow from Forecast.Solar
    (free, no key), tuned to the array via SOLAR_KWP / SOLAR_TILT / SOLAR_AZIMUTH.
    Cached ~2 h (their free tier allows ~12 calls/h). Returns {'today','tomorrow'}
    in kWh, or None (not configured / no location / fetch failure)."""
    if lat is None or lon is None or not SOLAR_KWP:
        return None
    key = (round(lat, 2), round(lon, 2))
    cached = _solar_fc_cache.get(key)
    if cached and time_module.time() - cached[0] < 7200:
        return cached[1]
    dec = SOLAR_TILT if SOLAR_TILT is not None else 0       # flat roof default
    az = SOLAR_AZIMUTH if SOLAR_AZIMUTH is not None else 0  # 0 = south (moot when flat)
    url = f"https://api.forecast.solar/estimate/{lat:.4f}/{lon:.4f}/{dec:.0f}/{az:.0f}/{SOLAR_KWP:.3f}"
    try:
        whd = ((requests.get(url, timeout=10).json() or {}).get("result") or {}).get("watt_hours_day") or {}
        days = sorted(whd.keys())   # ISO date keys, earliest first = today, then tomorrow
        result = {"today": (whd[days[0]] / 1000.0) if len(days) > 0 else None,
                  "tomorrow": (whd[days[1]] / 1000.0) if len(days) > 1 else None}
        _solar_fc_cache[key] = (time_module.time(), result)
        return result
    except Exception as e:
        server_log("GET", f"solar forecast fetch failed: {e}", "warning")
        return cached[1] if cached else None

def fmt_clock(dt):
    """12-hour compact clock, e.g. '6:02a' / '8:47p'."""
    h = dt.hour % 12 or 12
    return f"{h}:{dt.minute:02d}{'a' if dt.hour < 12 else 'p'}"

# Synodic month and a reference new moon (2000-01-06 18:14 UTC) for phase math.
_SYNODIC = 29.530588853
_MOON_REF = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
_MOON_NAMES = ["New moon", "Waxing crescent", "First quarter", "Waxing gibbous",
               "Full moon", "Waning gibbous", "Last quarter", "Waning crescent"]

def moon_phase(dt):
    """(name, illumination %) for the given UTC datetime — pure date math, no API."""
    age = ((dt - _MOON_REF).total_seconds() / 86400) % _SYNODIC
    illum = round((1 - math.cos(2 * math.pi * age / _SYNODIC)) / 2 * 100)
    # 8 phases centered on the named points; +0.5 so each name spans an eighth.
    name = _MOON_NAMES[int((age / _SYNODIC) * 8 + 0.5) % 8]
    return name, illum

# US AQI bands -> (upper bound, pill color, label). Our pills are green/yellow/red,
# so the unhealthy tiers all read red but keep their distinct EPA labels.
AQI_BANDS = [
    (50, "green", "Good"), (100, "yellow", "Moderate"),
    (150, "red", "Unhealthy (sensitive)"), (200, "red", "Unhealthy"),
    (300, "red", "Very unhealthy"), (10**9, "red", "Hazardous"),
]
_aqi_cache = {}  # (lat, lon) -> (epoch, data)

def get_air_quality(lat, lon):
    """Current US AQI + PM2.5 from Open-Meteo's air-quality API (free), cached ~20 min.
    PM2.5 is the wildfire-smoke proxy. Returns the parsed dict or None."""
    if lat is None or lon is None:
        return None
    key = (round(lat, 2), round(lon, 2))
    cached = _aqi_cache.get(key)
    if cached and time_module.time() - cached[0] < 1200:
        return cached[1]
    try:
        resp = requests.get("https://air-quality-api.open-meteo.com/v1/air-quality", params={
            "latitude": lat, "longitude": lon, "current": "us_aqi,pm2_5", "timezone": "auto",
        }, timeout=8)
        data = resp.json()
        _aqi_cache[key] = (time_module.time(), data)
        return data
    except Exception as e:
        server_log("GET", f"air quality fetch failed: {e}", "warning")
        return cached[1] if cached else None

# Order alerts by NWS severity so we surface the worst one.
SEVERITY_RANK = {"Extreme": 4, "Severe": 3, "Moderate": 2, "Minor": 1, "Unknown": 0}
NWS_HEADERS = {"User-Agent": "DeltaPI/1.0 (https://github.com/techdog21/deltapi)",
               "Accept": "application/geo+json"}
_alerts_cache = {}  # (lat, lon) -> (epoch, list)

def get_nws_alerts(lat, lon):
    """Active NWS watches/warnings for a point (free, US only), cached ~10 min.
    Returns a list of alert 'properties' dicts ([] = all clear), or None on
    failure / no location. NWS requires a descriptive User-Agent."""
    if lat is None or lon is None:
        return None
    key = (round(lat, 2), round(lon, 2))
    cached = _alerts_cache.get(key)
    if cached and time_module.time() - cached[0] < 600:
        return cached[1]
    try:
        resp = requests.get("https://api.weather.gov/alerts/active",
                            params={"point": f"{lat},{lon}"}, headers=NWS_HEADERS, timeout=8)
        alerts = [f.get("properties", {}) for f in ((resp.json() or {}).get("features") or [])]
        _alerts_cache[key] = (time_module.time(), alerts)
        return alerts
    except Exception as e:
        server_log("GET", f"nws alerts fetch failed: {e}", "warning")
        return cached[1] if cached else None

# ---- Geo helpers (miles + compass bearing) for proximity-based enrichment ----
def _haversine_mi(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles between two lat/lon points."""
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))

_COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
def _bearing(lat1, lon1, lat2, lon2):
    """Compass heading (N/NE/.../NW) from point 1 toward point 2."""
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
         - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(dl))
    brg = (math.degrees(math.atan2(y, x)) + 360) % 360
    return _COMPASS[int((brg + 22.5) // 45) % 8]

_place_cache = {}  # (lat, lon) -> (epoch, label)

def get_place(lat, lon):
    """Coarse 'Town, ST' label for a point via BigDataCloud's free no-key
    reverse-geocode API, cached ~6 h. Returns a string or None."""
    if lat is None or lon is None:
        return None
    key = (round(lat, 2), round(lon, 2))
    cached = _place_cache.get(key)
    if cached and time_module.time() - cached[0] < 21600:
        return cached[1]
    try:
        d = requests.get("https://api.bigdatacloud.net/data/reverse-geocode-client",
                         params={"latitude": lat, "longitude": lon, "localityLanguage": "en"},
                         timeout=8).json() or {}
        town = d.get("city") or d.get("locality") or ""
        code = d.get("principalSubdivisionCode") or ""    # e.g. 'US-UT'
        st = code.split("-")[-1] if "-" in code else (d.get("principalSubdivision") or "")
        label = ", ".join(p for p in (town, st) if p) or d.get("countryName") or None
        _place_cache[key] = (time_module.time(), label)
        return label
    except Exception as e:
        server_log("GET", f"reverse geocode failed: {e}", "warning")
        return cached[1] if cached else None

_fire_cache = {}  # (lat, lon) -> (epoch, dict)

def get_wildfires(lat, lon):
    """Active fire detections near a point (NASA FIRMS, VIIRS NOAA-20 NRT, last
    24 h), cached ~30 min. Needs a free FIRMS_MAP_KEY. Returns
    {'nearest_mi','bearing','count'} (count within 60 mi), {} when none nearby,
    or None (no key / no location / fetch failure)."""
    if lat is None or lon is None or not FIRMS_MAP_KEY:
        return None
    key = (round(lat, 1), round(lon, 1))
    cached = _fire_cache.get(key)
    if cached and time_module.time() - cached[0] < 1800:
        return cached[1]
    w, s, e, n = lon - 1.5, lat - 1.5, lon + 1.5, lat + 1.5    # ~100 mi box
    url = (f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{FIRMS_MAP_KEY}"
           f"/VIIRS_NOAA20_NRT/{w:.3f},{s:.3f},{e:.3f},{n:.3f}/1")
    try:
        rows = [r for r in requests.get(url, timeout=10).text.splitlines() if r.strip()]
        result = {}
        if len(rows) > 1 and "," in rows[0]:
            hdr = rows[0].split(",")
            li, lo = hdr.index("latitude"), hdr.index("longitude")
            ci = hdr.index("confidence") if "confidence" in hdr else None
            pts = []
            for row in rows[1:]:
                c = row.split(",")
                if ci is not None and len(c) > ci and c[ci].strip().lower() in ("l", "low"):
                    continue                                  # drop low-confidence detections
                try:
                    pts.append((float(c[li]), float(c[lo])))
                except (ValueError, IndexError):
                    continue
            if pts:
                dists = sorted((_haversine_mi(lat, lon, a, b), a, b) for a, b in pts)
                d0, a0, b0 = dists[0]
                result = {"nearest_mi": d0, "bearing": _bearing(lat, lon, a0, b0),
                          "count": sum(1 for d, _, _ in dists if d <= 60)}
        _fire_cache[key] = (time_module.time(), result)
        return result
    except Exception as e:
        server_log("GET", f"FIRMS fetch failed: {e}", "warning")
        return cached[1] if cached else None

_quake_cache = {}  # (lat, lon) -> (epoch, dict)

def get_quake(lat, lon):
    """Most significant earthquake within ~250 mi over the last 24 h (USGS),
    cached ~15 min. Returns {'mag','dist_mi','bearing','mins_ago','place'},
    {} when none, or None (no location / fetch failure)."""
    if lat is None or lon is None:
        return None
    key = (round(lat, 1), round(lon, 1))
    cached = _quake_cache.get(key)
    if cached and time_module.time() - cached[0] < 900:
        return cached[1]
    try:
        start = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        feats = (requests.get("https://earthquake.usgs.gov/fdsnws/event/1/query", params={
            "format": "geojson", "latitude": lat, "longitude": lon, "maxradiuskm": 400,
            "minmagnitude": 2.5, "orderby": "magnitude", "limit": 20, "starttime": start,
        }, timeout=8).json() or {}).get("features") or []
        result = {}
        for f in feats:                              # orderby=magnitude -> largest first
            p = f.get("properties") or {}
            g = (f.get("geometry") or {}).get("coordinates") or []
            mag = p.get("mag")
            if mag is None or len(g) < 2 or g[0] is None:
                continue
            mins = None
            if p.get("time"):
                mins = max(0.0, (datetime.now(timezone.utc).timestamp() - p["time"] / 1000) / 60)
            result = {"mag": mag, "dist_mi": _haversine_mi(lat, lon, g[1], g[0]),
                      "bearing": _bearing(lat, lon, g[1], g[0]), "mins_ago": mins,
                      "place": p.get("place")}
            break
        _quake_cache[key] = (time_module.time(), result)
        return result
    except Exception as e:
        server_log("GET", f"USGS quake fetch failed: {e}", "warning")
        return cached[1] if cached else None

_aurora_cache = {}  # (lat, lon) -> (epoch, dict)

def get_aurora(lat, lon):
    """Overhead aurora probability (NOAA SWPC OVATION 30-min forecast) and the
    latest planetary Kp index, cached ~20 min. Returns {'prob','kp'} or None.
    The OVATION grid is lon-major (lon 0-359, lat -90..90), so the cell index is
    lon*181 + (lat+90)."""
    if lat is None or lon is None:
        return None
    key = (round(lat, 1), round(lon, 1))
    cached = _aurora_cache.get(key)
    if cached and time_module.time() - cached[0] < 1200:
        return cached[1]
    try:
        coords = (requests.get("https://services.swpc.noaa.gov/json/ovation_aurora_latest.json",
                               timeout=10).json() or {}).get("coordinates") or []
        glat, glon = int(round(lat)), int(round(lon)) % 360
        prob = None
        idx = glon * 181 + (glat + 90)
        if 0 <= idx < len(coords) and coords[idx][0] == glon and coords[idx][1] == glat:
            prob = coords[idx][2]
        else:
            for c in coords:                          # fallback if the layout ever changes
                if c[0] == glon and c[1] == glat:
                    prob = c[2]
                    break
        kp = None
        try:
            kpd = requests.get("https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json",
                               timeout=8).json() or []
            last = kpd[-1] if kpd else None
            if isinstance(last, dict):
                kp = last.get("Kp")
            elif isinstance(last, list) and len(last) > 1:
                kp = float(last[1])
        except Exception:
            pass
        result = {"prob": prob, "kp": kp}
        _aurora_cache[key] = (time_module.time(), result)
        return result
    except Exception as e:
        server_log("GET", f"aurora fetch failed: {e}", "warning")
        return cached[1] if cached else None

# NWS flood categories (NWPS observed.floodCategory) -> human label
FLOOD_LABELS = {"no_flooding": "No flooding", "action": "Action stage", "minor": "Minor flood",
                "moderate": "Moderate flood", "major": "Major flood"}
_river_cache = {}  # (lat, lon) -> (epoch, dict)

def get_river(lat, lon):
    """Nearest active river gauge (USGS instantaneous gage height) enriched with
    its NWS flood category (NOAA NWPS, looked up by USGS site id), cached ~30 min.
    Returns {'name','stage','flood'}, {} when no gauge nearby, or None (no
    location / fetch failure)."""
    if lat is None or lon is None:
        return None
    key = (round(lat, 2), round(lon, 2))
    cached = _river_cache.get(key)
    if cached and time_module.time() - cached[0] < 1800:
        return cached[1]
    d = 0.6   # ~40 mi half-box
    try:
        ts = ((requests.get("https://waterservices.usgs.gov/nwis/iv/", params={
            "format": "json", "siteStatus": "active", "parameterCd": "00065",
            "bBox": f"{lon-d:.3f},{lat-d:.3f},{lon+d:.3f},{lat+d:.3f}",
        }, timeout=10).json() or {}).get("value") or {}).get("timeSeries") or []
        best = None  # (dist, name, stage_ft, usgs_id)
        for sct in ts:
            si = sct.get("sourceInfo") or {}
            gl = (si.get("geoLocation") or {}).get("geogLocation") or {}
            slat, slon = gl.get("latitude"), gl.get("longitude")
            if slat is None or slon is None:
                continue
            vals = ((sct.get("values") or [{}])[0].get("value")) or []
            try:
                stage = float(vals[-1]["value"]) if vals else None
            except (ValueError, KeyError, IndexError, TypeError):
                stage = None
            sid = ((si.get("siteCode") or [{}])[0].get("value"))
            dist = _haversine_mi(lat, lon, slat, slon)
            if best is None or dist < best[0]:
                best = (dist, si.get("siteName"), stage, sid)
        result = {}
        if best:
            _, name, stage, sid = best
            flood = None
            if sid:                                   # bridge USGS site -> NWPS flood category
                try:
                    obs = ((requests.get(f"https://api.water.noaa.gov/nwps/v1/gauges/{sid}",
                            timeout=8).json() or {}).get("status") or {}).get("observed") or {}
                    flood = obs.get("floodCategory")
                except Exception:
                    pass
            result = {"name": name, "stage": stage, "flood": flood}
        _river_cache[key] = (time_module.time(), result)
        return result
    except Exception as e:
        server_log("GET", f"river fetch failed: {e}", "warning")
        return cached[1] if cached else None

def _sl_up(sl):
    """True when Starlink is actively connected (basis for the up/down streak)."""
    return bool(sl) and bool(sl.get("ok")) and sl.get("state") == "CONNECTED" and not sl.get("currently_obstructed")

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

    # Voltage / power charts
    voltage_timestamps, voltage_values = build_voltage_series(parsed_chrono)
    timestamps = [fmt_mt(p[0]) for p in reversed(parsed)]
    powers = [p[3] for p in reversed(parsed)]                          # PPV (panel input)
    charge_powers = [round(p[1] * p[2], 1) for p in reversed(parsed)]  # MPPT output to battery (V*I)
    today_yield = parsed[0][8] if parsed else 0                        # H20, kWh produced today

    # Battery charts (measured) — chronological; only frames carrying battery data.
    batt_chrono = list(reversed(batt_series))
    batt_times = [fmt_mt(b[0]) for b in batt_chrono]
    batt_soc_values = [b[1] for b in batt_chrono]
    batt_load_values = [b[2] for b in batt_chrono]
    batt_temp_values = [b[3] for b in batt_chrono]
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

    # How long Starlink has held its current up/down state: walk frame history
    # (rows are newest-first) until the connected/not-connected state flips.
    sl_streak_html = ""
    if starlink:
        cur_up = _sl_up(starlink)
        streak_start, hit_edge = None, True
        for r_ts, _r_recv, r_data in rows:
            try:
                sl = json.loads(r_data).get("starlink")
            except Exception:
                continue
            if not sl:
                continue
            if _sl_up(sl) == cur_up:
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

    # ---- Weather location: dish GPS (Mini) -> home dish id -> home coords; else unknown ----
    dish_id = (starlink or {}).get("id")
    dlat, dlon = (starlink or {}).get("lat"), (starlink or {}).get("lon")
    if dlat is not None:
        wx_lat, wx_lon = dlat, dlon                      # dish sharing GPS (Mini on the road)
    elif HOME_DISH_ID and dish_id and dish_id != HOME_DISH_ID:
        wx_lat = wx_lon = None                           # positively roaming on a no-GPS dish
    else:
        wx_lat, wx_lon = HOME_LAT, HOME_LON              # home / can't tell -> home coords
    wx = get_weather(wx_lat, wx_lon)
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

    # Location (reverse-geocoded dish GPS) as the Weather panel's first metric.
    place = get_place(wx_lat, wx_lon)
    if place:
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

    alerts = get_nws_alerts(wx_lat, wx_lon)
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
    fires = get_wildfires(wx_lat, wx_lon)
    if fires is None:
        environment_html += '<div class="metric"><span class="metric-label">Wildfire</span><span class="metric-value"><span class="pill gray">—</span></span></div>'
    elif not fires:
        environment_html += '<div class="metric"><span class="metric-label">Wildfire</span><span class="metric-value"><span class="pill green">None within 60 mi</span></span></div>'
    else:
        nm, brg, cnt = fires["nearest_mi"], fires["bearing"], fires["count"]
        f_cls, f_lbl = ("red", "Close") if nm <= 10 else ("yellow", "Nearby") if nm <= 50 else ("green", "Distant")
        more = f" · {cnt} within 60 mi" if cnt > 1 else ""
        environment_html += f'<div class="metric"><span class="metric-label">Wildfire</span><span class="metric-value">{nm:.0f} mi {brg}{more} <span class="pill {f_cls}">{f_lbl}</span></span></div>'

    aq = (get_air_quality(wx_lat, wx_lon) or {}).get("current") or {}
    aqi, pm = aq.get("us_aqi"), aq.get("pm2_5")
    if aqi is None:
        environment_html += '<div class="metric"><span class="metric-label">Air quality</span><span class="metric-value"><span class="pill gray">—</span></span></div>'
    else:
        aq_cls, aq_lbl = next((c, l) for mx, c, l in AQI_BANDS if aqi <= mx)
        pm_str = f" · PM2.5 {pm:.0f}" if pm is not None else ""
        environment_html += f'<div class="metric"><span class="metric-label">Air quality</span><span class="metric-value">AQI {aqi:.0f}{pm_str} <span class="pill {aq_cls}">{aq_lbl}</span></span></div>'

    # Recent earthquake nearby (USGS) — most significant within ~250 mi over 24 h.
    quake = get_quake(wx_lat, wx_lon)
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
    river = get_river(wx_lat, wx_lon)
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
    aurora = get_aurora(wx_lat, wx_lon)
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

    # Sustainability Outlook (Solar panel): fuse measured daily harvest vs
    # consumption, the current charge state, and the solar forecast into one
    # forward-looking state (Self-sufficient / Sustaining / Drawing down / Critical).
    ah = _avg_complete_days(h20_values)    # avg recent daily harvest (kWh)
    ac = _avg_complete_days(cons_values)   # avg recent daily consumption (kWh)
    net_w = battery.get("power") if batt_present else None   # + = charging
    charging_now = bool(is_charging) or (net_w is not None and net_w >= 0)

    # Solar production forecast (Forecast.Solar): array-specific predicted kWh.
    # When configured, refine the forecast tier from predicted kWh vs measured
    # consumption — far more honest than the array-agnostic radiation thresholds.
    solar_fc = get_solar_forecast(wx_lat, wx_lon)
    solar_fc_html = ""
    if solar_fc:
        today_kwh, tomo_kwh = solar_fc.get("today"), solar_fc.get("tomorrow")
        frac_left = sun_frac_left if sun_frac_left is not None else 1.0
        forward_kwh = max((today_kwh or 0) * frac_left, tomo_kwh or 0)   # today's remaining vs tomorrow
        if ac is not None and ac > 0:
            forecast_tier = "good" if forward_kwh >= 1.2 * ac else "fair" if forward_kwh >= 0.8 * ac else "poor"
        tomo_str = f"{tomo_kwh:.1f} kWh" if tomo_kwh is not None else "—"
        fc_cls = {"good": "green", "fair": "yellow", "poor": "red"}.get(forecast_tier, "gray")
        solar_fc_html = (f'<div class="metric"><span class="metric-label">Solar forecast</span>'
                         f'<span class="metric-value">{tomo_str} tomorrow <span class="pill {fc_cls}">{forecast_tier.title()}</span></span></div>')

    outlook_class, outlook_label = sustainability_outlook(
        batt_present,
        soc_percent if batt_present else None,
        charging_now,
        usable_wh,
        ah * 1000 if ah is not None else None,
        ac * 1000 if ac is not None else None,
        forecast_tier,
    )

    # Disk status
    data_percent, data_class, data_label = get_disk_status(DB_DIR)

    # ==================== BUILD HTML ====================
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DeltaPi Camping Monitor</title>
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
    <h1>DELTAPI CAMPING MONITOR</h1>
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
        <div class="metric"><span class="metric-label">Temperature</span><span class="metric-value">{batt_temp_str} <span class="pill {btemp_class}">{btemp_label}</span></span></div>
        <div class="metric"><span class="metric-label">Cell balance</span><span class="metric-value"><span class="pill {cell_class}">{cell_label}</span></span></div>
        <div class="metric"><span class="metric-label">Consumption</span><span class="metric-value">{house_load_str}</span></div>
        <div class="metric"><span class="metric-label">Runtime</span><span class="metric-value">{runtime_str}</span></div>
        <div class="metric"><span class="metric-label">Time to full</span><span class="metric-value">{ttf_str}</span></div>
    </div>

    <!-- Solar System -->
    <div class="panel">
        <h2>Solar System</h2>
        <div class="metric"><span class="metric-label">Solar data</span><span class="metric-value"><span class="pill {status_color}">{status_text}</span></span></div>
        <div class="metric"><span class="metric-label">Controller</span><span class="metric-value"><span class="pill {ctrl_class}">{ctrl_label}</span></span></div>
        <div class="metric"><span class="metric-label">Mode</span><span class="metric-value"><span class="pill {mode_class}">{mode_label}</span></span></div>
        <div class="metric"><span class="metric-label">Solar power</span><span class="metric-value">{solar_now} W ({charge_now_a:.1f} A) <span class="pill {solar_class}">{solar_label}</span></span></div>
        <div class="metric"><span class="metric-label">Yield today</span><span class="metric-value">{today_yield:.2f} kWh</span></div>
        {solar_fc_html}
        <div class="metric"><span class="metric-label">Panel V</span><span class="metric-value">{latest_vpv:.2f} V <span class="pill {vpv_color}">{vpv_message}</span></span></div>
        <div class="metric"><span class="metric-label">Sustainability Outlook</span><span class="metric-value"><span class="pill {outlook_class}">{outlook_label}</span></span></div>
    </div>

    <!-- Starlink -->
    <div class="panel">
        <h2>Starlink</h2>
        <div class="metric"><span class="metric-label">Status</span><span class="metric-value"><span class="pill {sl_status_class}">{sl_status_label}</span></span></div>
        {sl_streak_html}
        <div class="metric"><span class="metric-label">Obstruction</span><span class="metric-value">{sl_obs_str} <span class="pill {sl_obs_class}">{sl_obs_label}</span></span></div>
        <div class="metric"><span class="metric-label">Alerts</span><span class="metric-value"><span class="pill {sl_alert_class}">{sl_alert_label}</span></span></div>
        <div class="metric"><span class="metric-label">Speed</span><span class="metric-value">{sl_speed_str}</span></div>
        <div class="metric"><span class="metric-label">Latency</span><span class="metric-value">{sl_latency_str}</span></div>
    </div>

    <!-- Weather (Open-Meteo, dish location) -->
    <div class="panel">
        <h2>Weather</h2>
        {weather_html}
    </div>

    <!-- Environment: severe-weather alerts, air quality, solar window -->
    <div class="panel">
        <h2>Environment</h2>
        {environment_html}
    </div>

    <!-- Pi Health -->
    <div class="panel">"""

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
        <div class="metric"><span class="metric-label">Link</span><span class="metric-value"><span class="pill {controller_class}">{controller_label}</span></span></div>
        <div class="metric"><span class="metric-label">OS</span><span class="metric-value">{pi_os_val}</span></div>
        <div class="metric"><span class="metric-label">Uptime</span><span class="metric-value">{pi_uptime}</span></div>
        <div class="metric"><span class="metric-label">Last Check-in</span><span class="metric-value"><span class="pill {checkin_class}">{checkin_label}</span></span></div>
        <div class="metric"><span class="metric-label">CPU / Fan</span><span class="metric-value">{pi_cpu_temp} <span class="pill {temp_class}">{temp_label}</span> {pi_fan_speed} <span class="pill {fan_class}">{fan_label}</span></span></div>
        <div class="metric"><span class="metric-label">Updates</span><span class="metric-value">{pi_updates_val} <span class="pill {updates_class}">{updates_label}</span></span></div>
        <div class="metric"><span class="metric-label">Mem / Disk</span><span class="metric-value">{pi_memory} / {pi_disk}</span></div>
        <div class="metric"><span class="metric-label">Wi-Fi</span><span class="metric-value">{pi_ssid} {pi_wifi_signal} <span class="pill {wifi_class}">{wifi_label}</span></span></div>
        <div class="metric"><span class="metric-label">Data storage</span><span class="metric-value">{data_percent}% used <span class="pill {data_class}">{data_label}</span> · {existing_days} of 365 d</span></div>"""
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

    <!-- Chart: Charge Power to battery (MPPT output) -->
    <div class="chart-panel">
        <h2>Charge Power (W)</h2>
        <div class="chart-wrap"><canvas id="chartCharge"></canvas></div>
    </div>

    <!-- Chart: Battery Temperature (measured) -->
    <div class="chart-panel">
        <h2>Battery Temp (°F)</h2>
        <div class="chart-wrap"><canvas id="chartTemp"></canvas></div>
    </div>

    <!-- Chart: Daily Consumption (measured) -->
    <div class="chart-panel">
        <h2>Daily Consumption (kWh)</h2>
        <div class="chart-wrap"><canvas id="chartConsDaily"></canvas></div>
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

    chart_payload = {
        "timestamps": timestamps, "powers": powers, "charge_powers": charge_powers,
        "voltage_timestamps": voltage_timestamps, "voltage_values": voltage_values,
        "h20_days": h20_days, "h20_values": h20_values, "h20_ymax": h20_ymax,
        "batt_times": batt_times, "batt_soc_values": batt_soc_values,
        "batt_load_values": batt_load_values, "batt_temp_values": batt_temp_values,
        "cons_days": cons_days, "cons_values": cons_values,
        "SOC_DANGER": SOC_DANGER, "FREEZE_F": FREEZE_F,
    }
    html += f"""
            </tbody>
        </table>
    </div>
    <script id="dash-data" type="application/json">{json.dumps(chart_payload)}</script>
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

// Live in-place refresh: fetch the page, swap only the .dashboard contents (the
// header/controls live outside it, so the dropdown/scroll are untouched), then
// rebuild the charts from the fresh data island. No full reload -> no flash.
function refreshDashboard() {{
    fetch(window.location.pathname + window.location.search, {{ cache: 'no-store' }})
        .then(function(r) {{ return r.text(); }})
        .then(function(html) {{
            var doc = new DOMParser().parseFromString(html, 'text/html');
            var fresh = doc.querySelector('.dashboard');
            var cur = document.querySelector('.dashboard');
            if (fresh && cur) {{ cur.innerHTML = fresh.innerHTML; initCharts(); }}
        }})
        .catch(function() {{}});
}}

var _refreshTimer = null;
function setAutoRefresh(seconds) {{
    if (_refreshTimer) clearInterval(_refreshTimer);
    _refreshTimer = null;
    localStorage.setItem('autoRefresh', seconds);
    if (seconds > 0) {{
        _refreshTimer = setInterval(refreshDashboard, seconds * 1000);
    }}
}}
(function() {{
    var saved = localStorage.getItem('autoRefresh') || '0';
    var sel = document.getElementById('autoRefresh');
    if (sel) sel.value = saved;
    if (parseInt(saved) > 0) setAutoRefresh(parseInt(saved));
}})();

// ---- Charts: rebuilt from the #dash-data JSON island so AJAX refresh can redraw them ----
var _charts = [];
function initCharts() {{
    var el = document.getElementById('dash-data');
    if (!el || typeof Chart === 'undefined') return;
    var D = JSON.parse(el.textContent);
    var style = getComputedStyle(document.documentElement);
    var gridColor = style.getPropertyValue('--grid-color').trim();
    var textMuted = style.getPropertyValue('--text-muted').trim();
    var cv = function(n) {{ return style.getPropertyValue(n).trim(); }};
    var chartOpts = function(yMin, yMax, stepSize) {{ return {{
        responsive: true, maintainAspectRatio: false, animation: false,
        plugins: {{ legend: {{ display: false }} }},
        elements: {{ point: {{ radius: 0 }}, line: {{ borderWidth: 1.5 }} }},
        scales: {{ x: {{ display: false }},
            y: {{ min: yMin, max: yMax, ticks: {{ stepSize: stepSize, font: {{ size: 9 }}, color: textMuted }}, grid: {{ color: gridColor }} }} }}
    }}; }};
    var flat = function(arr, val) {{ return arr.map(function() {{ return val; }}); }};
    _charts.forEach(function(c) {{ c.destroy(); }});
    _charts = [];
    function mk(id, cfg) {{ var e = document.getElementById(id); if (e) _charts.push(new Chart(e, cfg)); }}
    mk('chartPower', {{ type: 'line', data: {{ labels: D.timestamps, datasets: [{{ data: D.powers, borderColor: cv('--chart-power'), fill: false, tension: 0.1 }}] }}, options: chartOpts(0, 305, 50) }});
    mk('chartVoltage', {{ type: 'line', data: {{ labels: D.voltage_timestamps, datasets: [{{ data: D.voltage_values, borderColor: cv('--chart-voltage'), backgroundColor: cv('--chart-voltage-fill'), fill: true, tension: 0.3, spanGaps: false }}] }}, options: chartOpts(12.5, 14.6, 0.5) }});
    mk('chartH20', {{ type: 'line', data: {{ labels: D.h20_days, datasets: [{{ data: D.h20_values, borderColor: cv('--chart-h20'), backgroundColor: cv('--chart-h20-fill'), fill: true, tension: 0.2, pointRadius: 2 }}] }}, options: chartOpts(0, D.h20_ymax, D.h20_ymax / 4) }});
    mk('chartSOC', {{ type: 'line', data: {{ labels: D.batt_times, datasets: [
        {{ data: D.batt_soc_values, borderColor: cv('--chart-voltage'), backgroundColor: cv('--chart-voltage-fill'), fill: true, tension: 0.3, pointRadius: 0 }},
        {{ data: flat(D.batt_times, D.SOC_DANGER), borderColor: cv('--pill-red'), borderDash: [4,3], fill: false, pointRadius: 0 }}
    ] }}, options: chartOpts(0, 100, 20) }});
    mk('chartLoad', {{ type: 'line', data: {{ labels: D.batt_times, datasets: [{{ data: D.batt_load_values, borderColor: cv('--chart-power'), fill: false, tension: 0.2, pointRadius: 0 }}] }}, options: chartOpts(0, null, null) }});
    mk('chartCharge', {{ type: 'line', data: {{ labels: D.timestamps, datasets: [{{ data: D.charge_powers, borderColor: cv('--chart-h21-border'), fill: false, tension: 0.1 }}] }}, options: chartOpts(0, null, null) }});
    mk('chartTemp', {{ type: 'line', data: {{ labels: D.batt_times, datasets: [
        {{ data: D.batt_temp_values, borderColor: cv('--chart-power'), fill: false, tension: 0.3, pointRadius: 0, spanGaps: true }},
        {{ data: flat(D.batt_times, D.FREEZE_F), borderColor: cv('--pill-red'), borderDash: [4,3], fill: false, pointRadius: 0 }}
    ] }}, options: chartOpts(null, null, null) }});
    mk('chartConsDaily', {{ type: 'bar', data: {{ labels: D.cons_days, datasets: [{{ data: D.cons_values, backgroundColor: cv('--chart-h21'), borderColor: cv('--chart-power'), borderWidth: 1 }}] }}, options: chartOpts(0, null, null) }});
}}
initCharts();
</script>
</body>
</html>"""

    return html

if __name__ == "__main__":
    app.run()