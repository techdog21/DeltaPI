#!/usr/bin/env python3
"""
DeltaPI Renogy Battery BLE Poller
---------------------------------
Reads the Renogy Pro smart batteries over Bluetooth and writes their combined
state to a small JSON file (battery_state.json) that vedirect_logger.py merges
into its uploads. Runs as its own systemd service so any Bluetooth flakiness is
fully isolated from the rock-solid serial logging / upload path.

Each cycle connects to each battery, reads once, then disconnects — so the link
is only held briefly (leaving the batteries free for the phone app between
polls) and a single bad read never wedges the service.

The state file always carries a UTC `timestamp` and a `healthy` flag so the
dashboard can show whether the battery feed is live, stale, or down instead of
silently trusting old numbers.

Uses the vendored, patched `renogybt` package sitting next to this file.
"""
import os
import sys
import json
import time
import logging
import threading
import asyncio
import subprocess
from datetime import datetime, timezone

# find the vendored renogybt package regardless of cwd
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import configparser
from renogybt import BatteryClient

# ------------------ Configuration ------------------ #
STATE_DIR = os.environ.get("BATT_STATE_DIR", "/var/log/vedirect")
STATE_PATH = os.path.join(STATE_DIR, "battery_state.json")
POLL_INTERVAL = int(os.environ.get("BATT_POLL_INTERVAL", "60"))  # seconds between cycles
PER_BATT_TIMEOUT = int(os.environ.get("BATT_READ_TIMEOUT", "40"))  # per-battery read budget
READ_ATTEMPTS = int(os.environ.get("BATT_READ_ATTEMPTS", "3"))  # retries if a BLE scan misses a battery
GRACE_S = int(os.environ.get("BATT_GRACE", "300"))  # keep a battery's last-good reading this long if it misses
ADAPTER = os.environ.get("BATT_ADAPTER", "hci0")

# Last-good reading per battery id, so a single missed scan doesn't flip the
# whole feed to "degraded" (and the dashboard to the way-off estimate).
_last_good = {}


def clear_link(mac):
    """Drop any existing BLE connection to this MAC. A process killed mid-connect
    (e.g. by `systemctl restart`) can leave a battery stuck 'connected' so it stops
    advertising and can't be re-discovered; disconnecting frees it."""
    try:
        subprocess.run(["bluetoothctl", "disconnect", mac], timeout=10, capture_output=True)
    except Exception:
        pass

# (label, MAC, BLE alias). Alias must match the advertised name.
BATTERIES = [
    ("batt1", "4C:E1:74:5A:A4:56", "RNGPRO12BATT48000138"),
    ("batt2", "4C:E1:74:5C:CF:B1", "RNGPRO12BATT48000219"),
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("renogy_ble")


def make_config(mac, alias):
    c = configparser.ConfigParser()
    c['device'] = {'adapter': ADAPTER, 'mac_addr': mac, 'alias': alias,
                   'type': 'RNG_BATT', 'device_id': '255'}
    c['data'] = {'enable_polling': 'false', 'poll_interval': '60',
                 'temperature_unit': 'F', 'fields': ''}
    # sections required by the library's config parsing, kept disabled
    c['remote_logging'] = {'enabled': 'false', 'url': '', 'auth_header': ''}
    c['mqtt'] = {'enabled': 'false', 'server': '', 'port': '1883',
                 'topic': '', 'user': '', 'password': ''}
    c['pvoutput'] = {'enabled': 'false', 'api_key': '', 'system_id': ''}
    return c


def read_battery(mac, alias):
    """Connect, read once, return the raw parsed dict (or None on failure).
    Runs the library's blocking client in a worker thread with its own event
    loop, bounded by PER_BATT_TIMEOUT so a hung BLE op can't stall the service."""
    result = {'data': None}

    def on_data(client, data):
        result['data'] = data
        try: client.stop()
        except Exception: pass

    def on_error(client, error):
        try: client.stop()
        except Exception: pass

    def run():
        try:
            asyncio.set_event_loop(asyncio.new_event_loop())  # library uses get_event_loop()
            BatteryClient(make_config(mac, alias), on_data, on_error).start()
        except Exception as e:
            log.warning(f"{alias}: read exception: {e!r}")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(PER_BATT_TIMEOUT)
    if t.is_alive():
        log.warning(f"{alias}: read timed out after {PER_BATT_TIMEOUT}s")
    return result['data']


def summarize(label, mac, alias, raw):
    """Reduce a raw battery dict to the fields we publish."""
    try:
        v = float(raw['voltage'])
        cur = float(raw['current'])            # signed: + charging, - discharging
        rem = float(raw['remaining_charge'])   # Ah
        cap = float(raw['capacity'])           # Ah (current full-charge capacity)
        return {
            'label': label, 'mac': mac, 'id': alias[-3:], 'ok': True,
            'soc': round(100 * rem / cap, 1) if cap else None,
            'voltage': round(v, 2),
            'current': round(cur, 2),
            'power': round(v * cur, 1),
            'remaining_ah': round(rem, 2),
            'capacity_ah': round(cap, 2),
            'temps_f': [raw[k] for k in sorted(raw) if k.startswith('temperature_')],
            'cells_v': [raw[k] for k in sorted(raw) if k.startswith('cell_voltage_')],
        }
    except Exception as e:
        log.warning(f"{label}: parse error: {e}")
        return {'label': label, 'mac': mac, 'id': alias[-3:], 'ok': False}


def poll_once():
    batteries = []
    for label, mac, alias in BATTERIES:
        bid = alias[-3:]
        raw = None
        for attempt in range(1, READ_ATTEMPTS + 1):
            raw = read_battery(mac, alias)
            if raw:
                break
            # a single scan occasionally misses a battery; clear any stale link and re-scan
            log.warning("%s: read attempt %d/%d failed%s", alias, attempt, READ_ATTEMPTS,
                        "; retrying" if attempt < READ_ATTEMPTS else "")
            clear_link(mac)
            time.sleep(2)
        if raw:
            s = summarize(label, mac, alias, raw)
            _last_good[bid] = (time.time(), s)
            batteries.append(s)
        else:
            prev = _last_good.get(bid)
            age = (time.time() - prev[0]) if prev else None
            if prev and age < GRACE_S:
                # ride out a transient miss with the last-good reading (still counts ok)
                s = dict(prev[1]); s["stale_s"] = int(age)
                batteries.append(s)
                log.info("%s: miss; using last-good (%ds old)", alias, int(age))
            else:
                batteries.append({'label': label, 'mac': mac, 'id': bid, 'ok': False})
        time.sleep(1)  # brief gap between BLE connects

    ok = [b for b in batteries if b.get('ok')]
    bank = None
    if ok:
        rem = sum(b['remaining_ah'] for b in ok)
        cap = sum(b['capacity_ah'] for b in ok)
        cur = sum(b['current'] for b in ok)
        volt = round(sum(b['voltage'] for b in ok) / len(ok), 2)  # parallel: shared rail
        bank = {
            'soc': round(100 * rem / cap, 1) if cap else None,
            'voltage': volt,
            'current': round(cur, 2),
            'power': round(volt * cur, 1),  # + charging, - discharging (= -load)
            'remaining_ah': round(rem, 2),
            'capacity_ah': round(cap, 2),
            'battery_count': len(ok),
        }
    return {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'healthy': len(ok) == len(BATTERIES),
        'ok_count': len(ok),
        'total': len(BATTERIES),
        'bank': bank,
        'batteries': batteries,
    }


def write_state(state):
    tmp = STATE_PATH + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)  # atomic for the logger reading it concurrently


def main():
    log.info("Starting renogy_ble poller -> %s every %ss", STATE_PATH, POLL_INTERVAL)
    for _, mac, _ in BATTERIES:
        clear_link(mac)  # clear any stale links left by a previous unclean exit/restart
    while True:
        start = time.time()
        try:
            state = poll_once()
            write_state(state)
            b = state['bank']
            log.info("healthy=%s ok=%d/%d bank=%s", state['healthy'],
                     state['ok_count'], state['total'],
                     {k: b[k] for k in ('soc', 'voltage', 'current', 'power')} if b else None)
        except Exception as e:
            log.error("poll cycle failed: %r", e)
        time.sleep(max(5, POLL_INTERVAL - (time.time() - start)))


if __name__ == "__main__":
    main()
