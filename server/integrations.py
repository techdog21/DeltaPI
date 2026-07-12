"""
DeltaPi — external data providers.

Each provider is a cached, failure-tolerant (lat, lon) -> data lookup against a
free public API: weather, air quality, severe-weather alerts, wildfire/quake/
aurora/river enrichment, and reverse geocoding.

Caching is two-tier via the @_cached decorator: an in-process dict for speed,
backed by the ext_cache SQLite table (on the persistent /data disk) so a fresh
deploy/restart starts warm instead of re-pulling every upstream. On fetch
failure the freshest stale entry is served (memory, then DB), so a flaky
upstream never breaks the dashboard.

A background daemon thread (start_background_refresh, called from app.py)
re-pulls providers shortly *before* their TTL expires — but only while the
dashboard has been viewed recently (note_view) — so a user request almost never
pays for a live upstream fetch.
"""
import json
import sqlite3
import threading
import time as time_module
from datetime import datetime, timedelta, timezone
from functools import wraps
import requests

from config import DB_PATH, FIRMS_MAP_KEY, NWS_HEADERS
from util import server_log, _haversine_mi, _bearing


# ------------------ Two-tier cache ------------------ #
def _db_get(provider, key):
    """(epoch, data) from the persistent tier, or None."""
    try:
        with sqlite3.connect(DB_PATH, timeout=5) as c:
            row = c.execute("SELECT epoch, data FROM ext_cache WHERE provider = ? AND loc_key = ?",
                            (provider, key)).fetchone()
        return (row[0], json.loads(row[1])) if row else None
    except Exception as e:
        server_log("DB", f"ext_cache read failed ({provider}): {e}", "warning")
        return None


def _db_put(provider, key, epoch, data):
    try:
        with sqlite3.connect(DB_PATH, timeout=5) as c:
            c.execute("INSERT OR REPLACE INTO ext_cache (provider, loc_key, epoch, data) VALUES (?,?,?,?)",
                      (provider, key, epoch, json.dumps(data)))
    except Exception as e:
        server_log("DB", f"ext_cache write failed ({provider}): {e}", "warning")


# The background refresher passes _ahead=True, which shrinks the effective TTL
# by this margin so entries are renewed shortly BEFORE they expire — a user
# request then always lands on a fresh cache instead of paying for the fetch.
_REFRESH_AHEAD = 420


def _cached(name, ttl, prec=2):
    """Decorator turning a raw fetcher(lat, lon) into a cached provider.

    Lookup order: in-memory (fast path) -> ext_cache table (survives restarts)
    -> live fetch (result written to both tiers). Any fetch failure or None
    result falls back to the freshest stale entry available, matching the old
    per-provider behavior. `prec` is the lat/lon rounding for the cache key."""
    def deco(fetch):
        mem = {}   # loc_key -> (epoch, data)

        @wraps(fetch)
        def wrapper(lat, lon, _ahead=False):
            if lat is None or lon is None:
                return None
            key = f"{round(lat, prec)},{round(lon, prec)}"
            now = time_module.time()
            eff_ttl = max(60, ttl - _REFRESH_AHEAD) if _ahead else ttl
            hit = mem.get(key)
            if hit and now - hit[0] < eff_ttl:
                return hit[1]
            row = _db_get(name, key)   # fresh entry from a previous process?
            if row and now - row[0] < eff_ttl:
                mem[key] = row
                return row[1]
            try:
                data = fetch(lat, lon)
            except Exception as e:
                server_log("GET", f"{name} fetch failed: {e}", "warning")
                data = None
            if data is None:           # no result -> serve the freshest stale copy
                stale = max((e for e in (hit, row) if e), key=lambda e: e[0], default=None)
                return stale[1] if stale else None
            mem[key] = (now, data)
            _db_put(name, key, now, data)
            return data

        wrapper._mem = mem   # exposed for tests/debugging
        return wrapper
    return deco


# ------------------ Providers ------------------ #
@_cached("weather", ttl=1200)
def get_weather(lat, lon):
    """Current + 2-day forecast from Open-Meteo (free, no API key), cached ~20 min.
    Returns the parsed dict or None (no location / fetch failure)."""
    resp = requests.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,dew_point_2m,cloud_cover,weather_code,wind_speed_10m,wind_gusts_10m,precipitation,cape",
        "minutely_15": "precipitation",
        "daily": "weather_code,temperature_2m_min,shortwave_radiation_sum,sunrise,sunset",
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
        "timezone": "auto", "forecast_days": 2,
    }, timeout=8)
    return resp.json()


@_cached("aqi", ttl=1200)
def get_air_quality(lat, lon):
    """Current US AQI + PM2.5 from Open-Meteo's air-quality API (free), cached ~20 min.
    PM2.5 is the wildfire-smoke proxy. Returns the parsed dict or None."""
    resp = requests.get("https://air-quality-api.open-meteo.com/v1/air-quality", params={
        "latitude": lat, "longitude": lon, "current": "us_aqi,pm2_5", "timezone": "auto",
    }, timeout=8)
    return resp.json()


@_cached("alerts", ttl=600)
def get_nws_alerts(lat, lon):
    """Active NWS watches/warnings for a point (free, US only), cached ~10 min.
    Returns a list of alert 'properties' dicts ([] = all clear), or None on
    failure / no location. NWS requires a descriptive User-Agent."""
    resp = requests.get("https://api.weather.gov/alerts/active",
                        params={"point": f"{lat},{lon}"}, headers=NWS_HEADERS, timeout=8)
    return [f.get("properties", {}) for f in ((resp.json() or {}).get("features") or [])]


@_cached("place", ttl=21600)
def get_place(lat, lon):
    """Coarse 'Town, ST' label for a point via BigDataCloud's free no-key
    reverse-geocode API, cached ~6 h. Returns a string or None."""
    d = requests.get("https://api.bigdatacloud.net/data/reverse-geocode-client",
                     params={"latitude": lat, "longitude": lon, "localityLanguage": "en"},
                     timeout=8).json() or {}
    town = d.get("city") or d.get("locality") or ""
    code = d.get("principalSubdivisionCode") or ""    # e.g. 'US-UT'
    st = code.split("-")[-1] if "-" in code else (d.get("principalSubdivision") or "")
    return ", ".join(p for p in (town, st) if p) or d.get("countryName") or None


@_cached("fires", ttl=1800, prec=1)
def get_wildfires(lat, lon):
    """Active fire detections near a point (NASA FIRMS, VIIRS NOAA-20 NRT, last
    24 h), cached ~30 min. Needs a free FIRMS_MAP_KEY. Returns
    {'nearest_mi','bearing','count'} (count within 60 mi), {} when none nearby,
    or None (no key / no location / fetch failure)."""
    if not FIRMS_MAP_KEY:
        return None
    w, s, e, n = lon - 1.5, lat - 1.5, lon + 1.5, lat + 1.5    # ~100 mi box
    url = (f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{FIRMS_MAP_KEY}"
           f"/VIIRS_NOAA20_NRT/{w:.3f},{s:.3f},{e:.3f},{n:.3f}/1")
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
    return result


@_cached("quake", ttl=900, prec=1)
def get_quake(lat, lon):
    """Most significant earthquake within ~250 mi over the last 24 h (USGS),
    cached ~15 min. Returns {'mag','dist_mi','bearing','mins_ago','place'},
    {} when none, or None (no location / fetch failure)."""
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
    return result


@_cached("aurora", ttl=1200, prec=1)
def get_aurora(lat, lon):
    """Overhead aurora probability (NOAA SWPC OVATION 30-min forecast) and the
    latest planetary Kp index, cached ~20 min. Returns {'prob','kp'} or None.
    The OVATION grid is lon-major (lon 0-359, lat -90..90), so the cell index is
    lon*181 + (lat+90)."""
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
    return {"prob": prob, "kp": kp}


@_cached("river", ttl=1800)
def get_river(lat, lon):
    """Nearest active river gauge (USGS instantaneous gage height) enriched with
    its NWS flood category (NOAA NWPS, looked up by USGS site id), cached ~30 min.
    Returns {'name','stage','flood'}, {} when no gauge nearby, or None (no
    location / fetch failure)."""
    d = 0.6   # ~40 mi half-box
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
    return result


ALL_PROVIDERS = (get_weather, get_place, get_nws_alerts, get_wildfires,
                 get_air_quality, get_quake, get_river, get_aurora)


# ------------------ Background refresh ------------------ #
# Re-pull providers ahead of their TTLs so /external always lands on a fresh
# cache — but only while somebody is actually looking at the dashboard, out of
# courtesy to these free APIs. /external calls note_view() with the location it
# served; the loop goes dormant when the last view is older than _VIEW_WINDOW.
_REFRESH_INTERVAL = 300    # seconds between passes
_VIEW_WINDOW = 3600        # keep refreshing this long after the last dashboard view
_last_view = (0.0, None, None)   # (epoch, lat, lon)
_view_lock = threading.Lock()
_refresher_started = False


def note_view(lat, lon):
    """Record that the dashboard was just served for this location."""
    global _last_view
    if lat is None:
        return
    with _view_lock:
        _last_view = (time_module.time(), lat, lon)


def _refresh_pass():
    """One refresh sweep. Returns True if a location was refreshed (for tests)."""
    with _view_lock:
        ts, lat, lon = _last_view
    if lat is None or time_module.time() - ts > _VIEW_WINDOW:
        return False
    for fn in ALL_PROVIDERS:
        try:
            fn(lat, lon, _ahead=True)
        except Exception as e:   # belt-and-braces; providers already swallow failures
            server_log("REFRESH", f"{fn.__name__} background refresh failed: {e}", "warning")
    return True


def start_background_refresh():
    """Start the daemon refresh loop (idempotent; called once from app.py)."""
    global _refresher_started
    if _refresher_started:
        return
    _refresher_started = True

    def loop():
        while True:
            time_module.sleep(_REFRESH_INTERVAL)
            try:
                _refresh_pass()
            except Exception as e:
                server_log("REFRESH", f"refresh pass failed: {e}", "warning")

    threading.Thread(target=loop, daemon=True, name="ext-refresh").start()
