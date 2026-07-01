"""
DeltaPi — external data providers.

Each function is a cached, failure-tolerant (lat, lon) -> data lookup against a
free public API: weather, air quality, severe-weather alerts, wildfire/quake/
aurora/river enrichment, and reverse geocoding. All return None (or a cached
value) on failure so a flaky upstream never breaks the dashboard render.
"""
import time as time_module
from datetime import datetime, timedelta, timezone
import requests

from config import FIRMS_MAP_KEY, NWS_HEADERS
from util import server_log, _haversine_mi, _bearing

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
