"""
DeltaPI VE.Direct Logger and System Monitor
-------------------------------------------

This script is designed to run on a Raspberry Pi connected to a Victron VE.Direct solar charge controller. 
It reads serial data from the controller, logs it locally, and periodically uploads it to a remote Flask server. 
It also manages Raspberry Pi system health monitoring, including:

- CPU temperature-based fan control via PWM
- Wi-Fi signal strength and system metrics (disk, memory, uptime)
- Network-aware log uploading with offset tracking and pruning
- Robust error logging to a persistent file

Features:
- Local JSONL logging with automatic pruning of uploaded entries
- Bulk upload of VE.Direct data to a remote API every UPLOAD_INTERVAL seconds
- Periodic system health POSTs to `/status` endpoint
- PWM fan speed control targeting efficient and quiet cooling
- Designed for off-grid solar/RV monitoring setups

Log Files:
- VE.Direct data: /var/log/vedirect/solar_log.jsonl
- Errors:         /var/log/vedirect/vedirect_error.log
- Upload offset:  /var/log/vedirect/sent_offset.txt

Requirements:
- Python 3
- `requests`, `RPi.GPIO`, `pyserial`

Author: DeltaPI Project - Jerry Craft
"""

# Import necessary libraries
import serial
import os
import json
import time
import requests
import subprocess
import socket
from datetime import datetime, timedelta, timezone
import RPi.GPIO as GPIO

#  ------------------ Configuration ------------------ #
FAN_PIN = 18       # GPIO18 (physical pin 12)
ON_TEMP = 50       # °C (122°F) full speed at 50°C
OFF_TEMP = 30      # °C (77°F) fan off below 30°C
PWM_FREQ = 25000   # 25 kHz for silent PWM
FAN_MIN_DUTY = 20  # Minimum speed to reliably spin fan

# Log file paths
LOG_PATH = "/var/log/vedirect/solar_log.jsonl"
ERROR_LOG = "/var/log/vedirect/vedirect_error.log"
OFFSET_FILE = "/var/log/vedirect/sent_offset.txt"
POST_SECRET = os.environ.get("POST_SECRET")
if not POST_SECRET:
    print("[FATAL] POST_SECRET environment variable is not set. Exiting.")
    raise SystemExit(1)
# Ensure log directory exists
UPLOAD_INTERVAL = 30     # 30s — upload every other loop cycle
# Cap each bulk POST well under the server's MAX_CONTENT_LENGTH (1 MB). Without
# this, a backlog that ever grows past 1 MB (e.g. after a stretch of failed
# uploads) gets sent in a single oversized request that the server rejects with
# a 400 forever — the offset never advances, so the backlog can never drain.
MAX_UPLOAD_BYTES = 700_000
BASE_URL = os.environ.get("BASE_URL")
if not BASE_URL:
    print("[FATAL] BASE_URL environment variable is not set. Exiting.")
    raise SystemExit(1)
ARCHIVE_PATH = "/var/log/vedirect/solar_archive.jsonl"
MAX_ARCHIVE_DAYS = 14
ARCHIVE_PRUNE_INTERVAL = 86400  # rewrite/prune the archive at most once a day
_last_archive_prune = 0         # set on first prune so we don't rewrite every upload

# ------------------ Logging ------------------ #
def log_error(message):
    """
    Append a timestamped message to the error log file.

    Args:
        message (str): The message to log.
    """
    with open(ERROR_LOG, "a") as f:
        timestamp = datetime.now(timezone.utc).isoformat()
        f.write(f"[{timestamp}] {message}\n")


# ------------------ Network Check ------------------ #
def network_ready(host="8.8.8.8", port=53, timeout=3):
    """
    Check if the network is available by attempting a TCP connection.

    Args:
        host (str): Host to test connectivity (default: Google DNS 8.8.8.8).
        port (int): Port to test (default: 53).
        timeout (int): Timeout for the connection in seconds.

    Returns:
        bool: True if network is up, False otherwise.
    """
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


# ------------------ Fan Control ------------------ #
def setup_fan():
    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(FAN_PIN, GPIO.OUT)
        pwm = GPIO.PWM(FAN_PIN, PWM_FREQ)
        pwm.start(0)
        log_error("[Fan] Initialized at 0% duty cycle")
        return pwm, 0
    except Exception as e:
        log_error(f"[Fan] Not found or init failed: {e}")
        return None, 0

def update_fan(pwm, temp, current_duty):
    """
    Adjust fan speed for quiet operation targeting 40°C.
    Fan is completely off below OFF_TEMP. If temp is None (read failed),
    holds the current duty cycle unchanged.
    """
    if pwm is None or temp is None:
        return current_duty
    if temp <= OFF_TEMP:
        new_duty = 0  # Fan fully off
    elif temp >= ON_TEMP:
        new_duty = 100  # Full speed
    else:
        # Smooth ramp from FAN_MIN_DUTY to 100% between OFF_TEMP and ON_TEMP
        slope = (100 - FAN_MIN_DUTY) / (ON_TEMP - OFF_TEMP)
        new_duty = int(FAN_MIN_DUTY + slope * (temp - OFF_TEMP))

    # Only act and log on an actual change — logging every hold flooded the log
    # with a line every loop (the single biggest source of log growth).
    if new_duty != current_duty:
        pwm.ChangeDutyCycle(new_duty)

        if new_duty == 0:
            GPIO.output(FAN_PIN, GPIO.LOW)  # Ensure pin is LOW at 0%

        log_error(f"[Fan] Adjusted to {new_duty}% (CPU Temp: {temp}°C)")

    return new_duty


# ------------------ Serial Read ------------------ #
def read_frame(ser):
    """
    Read a frame of VE.Direct data from the serial port.

    Args:
        ser (Serial): The open serial connection.

    Returns:
        dict or None: Parsed key-value pairs from the frame,
                      or None if no complete frame received.
    """
    frame = {}
    empty_count = 0
    while True:
        line = ser.readline().decode('utf-8', errors='ignore').strip()
        if not line:
            empty_count += 1
            if empty_count >= 5:
                return None
            continue
        empty_count = 0
        if line.startswith("Checksum"):
            return frame
        parts = line.split(None, 1)
        if len(parts) == 2:
            key, value = parts
            frame[key] = value


# ------------------ Helpers ------------------ #
def get_pi_temp():
    """
    Get the current CPU temperature of the Raspberry Pi.

    Returns:
        float or None: CPU temperature in °C, or None if unavailable.
    """
    try:
        result = subprocess.check_output(["vcgencmd", "measure_temp"]).decode()
        temp_str = result.strip().split("=")[1].split("'")[0]
        return float(temp_str)
    except Exception:
        return None


def bulk_upload(entries):
    """
    Upload multiple log entries to the remote server in bulk.

    Args:
        entries (list): List of log entries (dicts).

    Returns:
        bool: True if upload succeeded, False otherwise.
    """
    if not network_ready():
        log_error("[Network] Offline: Skipping bulk upload")
        return False
    try:
        headers = {"Authorization": f"Bearer {POST_SECRET}"}
        resp = requests.post(BASE_URL + "/log/bulk", json=entries, headers=headers, timeout=10)
        if resp.status_code == 200:
            log_error(f"[Upload] Bulk POST succeeded: {resp.status_code}")
            return True
        else:
            log_error(f"[Upload] Bulk POST failed: {resp.status_code}")
            return False
    except Exception as e:
        log_error(f"[Upload] Bulk POST exception: {e}")
        return False


def get_last_sent_offset():
    """
    Retrieve the last sent log offset from the offset file.

    Returns:
        int: Last sent offset, or 0 if unavailable.
    """
    try:
        with open(OFFSET_FILE, "r") as f:
            return int(f.read().strip())
    except Exception:
        return 0


def update_last_sent_offset(offset):
    """
    Update the offset file with the new sent log offset.

    Args:
        offset (int): The new offset position.
    """
    try:
        with open(OFFSET_FILE, "w") as f:
            f.write(str(offset))
        log_error(f"[Offset] Updated to {offset}")
    except Exception as e:
        log_error(f"[Offset] Save failed: {e}")


def prune_sent_logs():
    """Remove already-uploaded entries from the log file using atomic swap.

    Note: this assumes a single-threaded writer. Lines appended to LOG_PATH
    between the read and the os.replace would be lost.
    """
    try:
        offset = get_last_sent_offset()
        if offset <= 0:
            return

        temp_path = LOG_PATH + ".tmp"
        with open(LOG_PATH, "r") as f:
            f.seek(offset)
            remaining = f.read()

        with open(temp_path, "w") as f:
            f.write(remaining)

        os.replace(temp_path, LOG_PATH)
        update_last_sent_offset(0)
        log_error("[Prune] Sent logs pruned successfully")
    except Exception as e:
        log_error(f"[Prune] Failed: {e}")


def upload_unsent_logs():
    """
    Upload log entries that were not sent previously.
    """
    if not network_ready():
        log_error("[Network] Offline: Skipping unsent log upload")
        return
    try:
        offset = get_last_sent_offset()
        entries = []
        batch_bytes = 0
        new_offset = offset
        with open(LOG_PATH, "r") as f:
            f.seek(offset)
            # Read line-by-line and stop once the accumulated payload nears the
            # size cap, so a large backlog is uploaded in several requests rather
            # than one oversized POST. Track the byte offset manually (rather than
            # f.tell(), which is unreliable when mixed with iteration) so prune
            # only drops the lines we actually sent; the rest go next cycle.
            for line in iter(f.readline, ""):
                if entries and batch_bytes + len(line) > MAX_UPLOAD_BYTES:
                    break
                new_offset += len(line.encode("utf-8"))
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
                batch_bytes += len(line)
            if entries and bulk_upload(entries):
                update_last_sent_offset(new_offset)
                archive_sent_logs()
                prune_sent_logs()

    except Exception as e:
        log_error(f"[Upload] Bulk error: {e}")


def get_wifi_signal_strength():
    """
    Get the current Wi-Fi signal strength in dBm.

    Returns:
        str: Signal strength as a string, or 'unknown' if unavailable.
    """
    try:
        result = subprocess.check_output(["iwconfig", "wlan0"]).decode()
        for line in result.splitlines():
            if "Signal level=" in line:
                dBm = line.strip().split("Signal level=")[1].split()[0].replace("dBm", "")
                return f"{dBm} dBm"
    except Exception as e:
        log_error(f"[WiFi] Signal fetch error: {e}")
    return "unknown"

def prune_archive():
    """Trim the archive to MAX_ARCHIVE_DAYS by rewriting it. This reads and
    rewrites the whole file, so it is throttled to once a day by the caller —
    doing it on every upload meant rewriting tens of MB every 30 s, which is
    needless SD-card wear."""
    try:
        if not os.path.exists(ARCHIVE_PATH):
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_ARCHIVE_DAYS)
        kept = []
        with open(ARCHIVE_PATH, "r") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    ts_str = entry.get("timestamp")
                    if not ts_str:
                        continue
                    ts = datetime.fromisoformat(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts >= cutoff:
                        kept.append(line)
                except (json.JSONDecodeError, ValueError, TypeError):
                    log_error(f"[Archive] Skipping malformed entry: {line.strip()[:80]}")
                    continue
        with open(ARCHIVE_PATH, "w") as f:
            f.writelines(kept)
        log_error(f"[Archive] Pruned archive, kept {len(kept)} entries within {MAX_ARCHIVE_DAYS} days")
    except Exception as e:
        log_error(f"[Archive] Prune failed: {e}")


def archive_sent_logs():
    """Append just-sent entries to the archive (cheap, append-only). The
    expensive 14-day prune is rewritten at most once a day, not every upload."""
    try:
        offset = get_last_sent_offset()
        if offset <= 0:
            return

        with open(LOG_PATH, "r") as f:
            sent_data = f.read(offset)
        with open(ARCHIVE_PATH, "a") as f:
            f.write(sent_data)

        global _last_archive_prune
        if time.time() - _last_archive_prune > ARCHIVE_PRUNE_INTERVAL:
            prune_archive()
            _last_archive_prune = time.time()
    except Exception as e:
        log_error(f"[Archive] Failed: {e}")


def send_pi_status(current_duty, fan_available=True, serial_connected=False):
    """
    Send the Raspberry Pi's system status and fan speed to the remote server.

    Args:
        current_duty (int): Current fan duty cycle (%).
        fan_available (bool): Whether the fan is available.
        serial_connected (bool): Whether the VE.Direct serial connection is active.
    """
    if not network_ready():
        log_error("[Network] Offline: Skipping Pi status POST")
        return

    def safe(cmd, parse):
        """Run a shell command and parse its output, returning 'unknown' on any failure."""
        try:
            return parse(subprocess.check_output(cmd).decode())
        except Exception:
            return "unknown"

    # Get uptime
    uptime = safe(["uptime", "-p"], lambda x: x.strip().replace("up ", ""))
    # Get CPU temperature
    cpu_temp = safe(["vcgencmd", "measure_temp"], lambda x: (
        lambda c: f"{c}°C / {round((float(c) * 9 / 5) + 32, 1)}°F"
    )(x.split("=")[1].replace("'C", "")))
    # Get disk usage
    disk = safe(["df", "-h", "/"], lambda x: f"{x.splitlines()[1].split()[2]}/{x.splitlines()[1].split()[1]} ({x.splitlines()[1].split()[4]})")
    # Get memory usage
    def parse_memory(output):
        """Parse 'free -m' output into 'used/total (percent%)' format."""
        row = output.splitlines()[1].split()
        total = int(row[1])
        used = int(row[2])
        if total == 0:
            return "0/0 (0%)"
        return f"{used}/{total} ({int(used / total * 100)}%)"
    memory = safe(["free", "-m"], parse_memory)
    # Get Wi-Fi SSID
    #ssid = safe(["iwgetid", "-r"], lambda x: x.strip() or "not connected")
    ssid = safe(["iwgetid", "-r"], lambda x: x.strip() or "not connected") + \
       " (" + safe(["hostname", "-I"], lambda x: x.strip().split()[0] if x.strip() else "no IP") + ")"

    # Get Wi-Fi signal strength
    wifi_signal = get_wifi_signal_strength()
    # Get hostname
    hostname = safe(["hostname"], lambda x: x.strip())
    # Get OS version
    os_version = safe(["lsb_release", "-d"], lambda x: x.strip().split(":")[1].strip() if ":" in x else x.strip())
    # Get number of available updates
    updates = safe(
        ["bash", "-c", "apt list --upgradeable 2>/dev/null | wc -l"],
        lambda x: str(int(x.strip()) - 1) + " available updates"
    )

    # Prepare payload for status POST
    payload = {
        "uptime": uptime,
        "cpu_temp": cpu_temp,
        "disk": disk,
        "memory": memory,
        "ssid": ssid,
        "wifi_signal": wifi_signal,
        "fan_speed": f"{current_duty}%" if fan_available else "N/A",
        "pi_name": hostname,
        "pi_os": os_version,
        "pi_updates": updates,
        "controller": "Connected" if serial_connected else "No controller detected"
    }

    try:
        # Send the status POST request
        headers = {"Authorization": f"Bearer {POST_SECRET}"}
        status_url = BASE_URL + "/status"
        # POST the status data
        r = requests.post(status_url, json=payload, headers=headers, timeout=5)
        # Check the response status
        if r.status_code == 200:
            log_error(f"[Status] POST succeeded: {r.status_code}")
        else:
            log_error(f"[Status] POST failed: {r.status_code} {r.text}")
    except Exception as e:
        log_error(f"[Status] Exception: {e}")

def backfill_from_archive():
    """If server has no data, replay local archive to repopulate after a redeploy."""
    if not network_ready():
        return

    try:
        entries = []
        if not os.path.exists(ARCHIVE_PATH):
            log_error("[Backfill] No archive file found")
            return

        with open(ARCHIVE_PATH, "r") as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue

        if not entries:
            log_error("[Backfill] Archive empty")
            return

        # Probe server with a single entry; dedup means a non-zero "inserted"
        # count tells us the server has no record of this timestamp yet.
        headers = {"Authorization": f"Bearer {POST_SECRET}"}
        resp = requests.post(
            BASE_URL + "/log/bulk",
            json=entries[:1],
            headers=headers,
            timeout=10
        )
        result = resp.json()

        # If server inserted it, server was empty — send everything
        if result.get("inserted", 0) > 0:
            log_error(f"[Backfill] Server looks fresh, replaying {len(entries)} archived entries")
            BATCH = 50
            for i in range(0, len(entries), BATCH):
                batch = entries[i:i + BATCH]
                bulk_upload(batch)
                time.sleep(1)  # Be gentle on rate limits
            log_error("[Backfill] Archive replay complete")
        else:
            log_error("[Backfill] Server already has data, skipping backfill")

    except Exception as e:
        log_error(f"[Backfill] Failed: {e}")

# ------------------ Main ------------------ #
def main():
    """
    Main loop for VE.Direct Logger:
    - Initializes fan control
    - Opens serial port
    - Reads and logs VE.Direct frames
    - Uploads data to the remote server
    - Monitors Pi health and controls fan
    """
    log_error("[System] Starting vedirect_logger")
    pwm, current_duty = setup_fan()

    SERIAL_RETRY_INTERVAL = 60  # seconds between reconnect attempts
    try:
        ser = None
        try:
            ser = serial.Serial('/dev/ttyUSB0', 19200, timeout=5)
            log_error("[Serial] Connected to /dev/ttyUSB0")
        except Exception as e:
            log_error(f"[Serial] Not available, running without controller: {e}")

        fan_available = pwm is not None
        upload_unsent_logs()
        backfill_from_archive()
        send_pi_status(current_duty, fan_available, serial_connected=(ser is not None))
        log_error("[System] Startup complete — initial status posted")
        last_prune_check = time.time()
        last_status_time = time.time()
        last_serial_retry = time.time()

        while True:
            try:
                # Retry serial connection periodically if not connected
                if ser is None and time.time() - last_serial_retry > SERIAL_RETRY_INTERVAL:
                    last_serial_retry = time.time()
                    try:
                        ser = serial.Serial('/dev/ttyUSB0', 19200, timeout=5)
                        log_error("[Serial] Reconnected to /dev/ttyUSB0")
                    except Exception:
                        pass  # stay in no-controller mode

                if ser is not None:
                    frame = read_frame(ser)
                    if frame:
                        frame["timestamp"] = datetime.now(timezone.utc).isoformat()
                        try:
                            with open(LOG_PATH, "a") as f:
                                json.dump(frame, f)
                                f.write("\n")
                        except Exception as e:
                            log_error(f"[Log] Local write failed: {e}")

                if time.time() - last_prune_check > UPLOAD_INTERVAL:
                    upload_unsent_logs()
                    last_prune_check = time.time()

                if time.time() - last_status_time > UPLOAD_INTERVAL:
                    send_pi_status(current_duty, fan_available, serial_connected=(ser is not None))
                    last_status_time = time.time()

                temp = get_pi_temp()
                current_duty = update_fan(pwm, temp, current_duty)

            except serial.SerialException as e:
                log_error(f"[Serial] Lost connection: {e}")
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
            except Exception as e:
                log_error(f"[Loop] Error: {e}")

            time.sleep(15)

    finally:
        if ser is not None:
            try:
                ser.close()
                log_error("[Serial] Connection closed")
            except Exception:
                pass
        if pwm is not None:
            pwm.stop()
            GPIO.cleanup()
        log_error("[System] PWM stopped and GPIO cleaned up")


if __name__ == "__main__":

    main()

