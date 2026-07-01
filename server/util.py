"""
DeltaPi — cross-cutting helpers with no domain or DB dependency.

Logging, small formatters/parsers, threshold pills, token decryption, moon-phase
math, and geo (distance/bearing) helpers shared across the server modules.
"""
import math
import logging
from datetime import datetime, timezone
from flask import request, has_request_context

from config import SERVER_LOG, MT, fernet, MAX_DAYS


# ------------------ Logging ------------------ #
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


# ------------------ Formatting / parsing ------------------ #
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


def ensure_utc(dt):
    """Assume UTC if datetime is naive (e.g. from SQLite)."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def fmt_mt(iso_str, fmt="%Y-%m-%d %H:%M"):
    """Convert an ISO timestamp string to Mountain Time for display."""
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MT).strftime(fmt)


def fmt_clock(dt):
    """12-hour compact clock, e.g. '6:02a' / '8:47p'."""
    h = dt.hour % 12 or 12
    return f"{h}:{dt.minute:02d}{'a' if dt.hour < 12 else 'p'}"


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


def _avg_complete_days(values, n=7):
    """Average of recent COMPLETE days, dropping the most recent (still-partial)
    day, over up to n days. Same unit in/out. None when history is too thin."""
    if not values or len(values) < 2:
        return None
    vals = values[:-1][-n:]          # drop today's partial value, keep last n complete days
    return sum(vals) / len(vals) if vals else None


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


# ------------------ Moon phase (pure date math, no API) ------------------ #
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


# ------------------ Geo helpers (miles + compass bearing) ------------------ #
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
