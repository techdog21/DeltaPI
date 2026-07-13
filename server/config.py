"""
DeltaPi — server configuration and static lookup tables.

Environment-derived settings (paths, secrets, location fallbacks, solar array
sizing) and the constant maps used to interpret VE.Direct / weather codes. This
module has no Flask dependency, so every other module can import from it freely.
"""
import os
import logging
from zoneinfo import ZoneInfo
from cryptography.fernet import Fernet

# ------------------ Paths / storage ------------------ #
DB_DIR = os.environ.get("DB_DIR", "/data")
DB_PATH = os.path.join(DB_DIR, "vedirect.db")
SERVER_LOG = os.path.join(DB_DIR, "server.log")
os.makedirs(DB_DIR, exist_ok=True)

# ------------------ Secrets ------------------ #
POST_SECRET = os.environ.get("POST_SECRET")
if not POST_SECRET:
    logging.warning("POST_SECRET is not set — all authenticated routes will reject requests")

# Separate secret for the dashboard's write actions (saving/selecting weather
# locations). Kept distinct from POST_SECRET on purpose: the browser holds this
# one, so leaking it can only change a location — never forge data ingestion.
# Fails closed: if unset, the /set_location and /add_location routes are disabled.
ADMIN_SECRET = os.environ.get("ADMIN_SECRET")
if not ADMIN_SECRET:
    logging.warning("ADMIN_SECRET is not set — dashboard location editing is disabled")

FERNET_KEY = os.environ.get("FERNET_KEY")
if not FERNET_KEY:
    logging.warning("FERNET_KEY is not set — /encrypt_days will be unavailable")
fernet = Fernet(FERNET_KEY.encode()) if FERNET_KEY else None
MAX_DAYS = 365  # matches the 365-day retention enforced by cleanup_old_records

# ------------------ Battery / runtime model ------------------ #
# The MPPT reports only charge current (never house load), so consumption is
# derived from energy balance instead — see estimate_avg_load_w(). Constants
# below were calibrated against ~14 days of field data (parked base ~5 W;
# woods-with-Starlink 24h average ~45-50 W).
BATTERY_CAPACITY_WH = 200 * 12   # 200 Ah @ 12 V nominal pack (estimate fallback only)
SOC_FLOOR = 10                   # don't plan usable capacity below ~10% on LiFePO4
LOAD_WINDOW_HOURS = 72           # trailing window for the empirical load estimate


def _env_float(name):
    try:
        return float(os.environ[name])
    except Exception:
        return None


# ------------------ Location / solar forecast ------------------ #
# Fallback weather location (set in the Render dashboard) used when the connected
# dish is the home dish (no GPS over the API). Kept out of the repo for privacy.
HOME_LAT = _env_float("HOME_LAT")
HOME_LON = _env_float("HOME_LON")
HOME_DISH_ID = os.environ.get("HOME_DISH_ID")  # round home dish id -> use HOME_LAT/LON

# Saved weather locations seeded into SQLite on a fresh DB. After that they're
# editable from the dashboard header dropdown (stored in the `locations` table),
# so this is only the initial set. Handy when the dish won't share GPS (e.g. the
# Starlink Mini): pick your spot from the dropdown instead of relying on the
# dish. West longitudes are negative. The first entry is selected on a fresh DB.
SEED_LOCATIONS = [
    {"name": "Grayback Gulch", "lat": 43.80673, "lon": -115.868826},  # Boise NF, past Idaho City
]
FIRMS_MAP_KEY = os.environ.get("FIRMS_MAP_KEY")  # NASA FIRMS wildfire detections (free signup)
SOLAR_KWP = _env_float("SOLAR_KWP")        # array peak kW for the solar forecast (e.g. 0.3 = 300 W)
SOLAR_PR = _env_float("SOLAR_PR") or 0.75  # performance ratio (system losses) for the kWh estimate

MT = ZoneInfo("America/Denver")

# ------------------ VE.Direct lookup tables ------------------ #
# Charge-state (CS) codes -> labels
CS_MAP = {
    "0": "Off",
    "1": "Low Power",
    "2": "Fault",
    "3": "Bulk",
    "4": "Absorption",
    "5": "Float",
}

# VE.Direct charger error codes (ERR) -> short labels; unmapped codes show "Error N"
ERR_MAP = {
    "0": "OK", "2": "Battery high V", "17": "Overtemp", "18": "Over-current",
    "19": "Current reversed", "20": "Bulk time exceeded", "21": "Sensor issue",
    "26": "Terminals hot", "28": "Power stage", "33": "PV over-voltage",
    "34": "PV over-current", "38": "PV shutdown (batt V)", "65": "Comm lost",
    "67": "BMS lost", "116": "Cal lost", "117": "Bad firmware", "119": "Settings lost",
}

# ------------------ Weather / environment lookup tables ------------------ #
# WMO weather codes -> short labels (Open-Meteo current/daily weather_code)
WMO_CODES = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog", 51: "Lt drizzle", 53: "Drizzle", 55: "Hvy drizzle",
    61: "Lt rain", 63: "Rain", 65: "Hvy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Lt snow", 73: "Snow", 75: "Hvy snow", 77: "Snow", 80: "Showers", 81: "Showers",
    82: "Hvy showers", 85: "Snow showers", 86: "Snow showers", 95: "Thunderstorm",
    96: "Thunderstorm", 99: "Thunderstorm",
}

# US AQI bands -> (upper bound, pill color, label). Our pills are green/yellow/red,
# so the unhealthy tiers all read red but keep their distinct EPA labels.
AQI_BANDS = [
    (50, "green", "Good"), (100, "yellow", "Moderate"),
    (150, "red", "Unhealthy (sensitive)"), (200, "red", "Unhealthy"),
    (300, "red", "Very unhealthy"), (10**9, "red", "Hazardous"),
]

# Order alerts by NWS severity so we surface the worst one.
SEVERITY_RANK = {"Extreme": 4, "Severe": 3, "Moderate": 2, "Minor": 1, "Unknown": 0}
NWS_HEADERS = {"User-Agent": "DeltaPI/1.0 (https://github.com/techdog21/deltapi)",
               "Accept": "application/geo+json"}

# NWS flood categories (NWPS observed.floodCategory) -> human label
FLOOD_LABELS = {"no_flooding": "No flooding", "action": "Action stage", "minor": "Minor flood",
                "moderate": "Moderate flood", "major": "Major flood"}
