#!/usr/bin/env python3
"""
DeltaPI Starlink Poller
-----------------------
Queries the Starlink dish's local gRPC API for connectivity status (and location,
once enabled in the Starlink app) and writes /var/log/vedirect/starlink_state.json,
which vedirect_logger.py merges into its uploads. Runs as its own systemd service so
dish/network issues never affect serial logging.

Dependencies (Pi-side venv): grpcio, yagrc, and the `starlink-grpc-tools` repo for
the `starlink_grpc` module (see deploy/starlink_poll.service). Not vendored — the
tool is large and its license is unclear, so we depend on a local clone instead.
"""
import os
import sys
import json
import time
import logging
from datetime import datetime, timezone

# Locate the cloned starlink-grpc-tools (override with STARLINK_TOOLS)
sys.path.insert(0, os.environ.get("STARLINK_TOOLS", os.path.expanduser("~/starlink-grpc-tools")))
import starlink_grpc

STATE_DIR = os.environ.get("BATT_STATE_DIR", "/var/log/vedirect")
STATE_PATH = os.path.join(STATE_DIR, "starlink_state.json")
POLL_INTERVAL = int(os.environ.get("STARLINK_POLL_INTERVAL", "60"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("starlink_poll")

# Only surface the actionable alerts (skip noisy/minor ones like signal-vs-predicted)
ALERT_LABELS = {
    "alert_mast_not_near_vertical": "Mast not vertical",
    "alert_thermal_throttle": "Thermal throttle",
    "alert_thermal_shutdown": "Thermal shutdown",
    "alert_motors_stuck": "Motors stuck",
    "alert_roaming": "Roaming",
    "alert_dish_water_detected": "Dish water",
    "alert_router_water_detected": "Router water",
    "alert_install_pending": "Install pending",
    "alert_no_ethernet_link": "No ethernet",
}


def poll_once():
    state = {"timestamp": datetime.now(timezone.utc).isoformat(), "ok": False}
    try:
        status, _obstruction, alerts = starlink_grpc.status_data()
    except Exception as e:
        log.warning("status query failed: %r", e)
        return state
    state.update({
        "ok": True,
        "id": status.get("id"),  # stable dish hardware id — lets the server detect "home"
        "state": status.get("state"),
        "obstruction_pct": round((status.get("fraction_obstructed") or 0) * 100, 2),
        "currently_obstructed": bool(status.get("currently_obstructed")),
        "down_mbps": round((status.get("downlink_throughput_bps") or 0) / 1e6, 1),
        "up_mbps": round((status.get("uplink_throughput_bps") or 0) / 1e6, 1),
        "latency_ms": round(status.get("pop_ping_latency_ms") or 0, 1),
        "ping_drop": round(status.get("pop_ping_drop_rate") or 0, 3),
        "gps_sats": status.get("gps_sats"),
        "alerts": [lbl for key, lbl in ALERT_LABELS.items() if alerts.get(key)],
    })
    # Location — null until enabled in the Starlink app ("Allow access on local network")
    try:
        loc = starlink_grpc.location_data()
        if loc.get("latitude") is not None:
            state["lat"] = round(loc["latitude"], 4)
            state["lon"] = round(loc["longitude"], 4)
    except Exception as e:
        log.debug("location not available: %r", e)
    return state


def write_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def main():
    log.info("Starting starlink_poll -> %s every %ss", STATE_PATH, POLL_INTERVAL)
    while True:
        t0 = time.time()
        try:
            st = poll_once()
            write_state(st)
            log.info("ok=%s state=%s obstruction=%s%% loc=%s",
                     st.get("ok"), st.get("state"), st.get("obstruction_pct"),
                     "yes" if "lat" in st else "no")
        except Exception as e:
            log.error("poll cycle failed: %r", e)
        time.sleep(max(5, POLL_INTERVAL - (time.time() - t0)))


if __name__ == "__main__":
    main()
