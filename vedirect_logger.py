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
- Immediate and bulk upload of VE.Direct data to a remote API
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
from datetime import datetime
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
POST_URL = "https://deltapi-k3bf.onrender.com/log"
POST_SECRET = os.environ.get("POST_SECRET", "deltapiproject123")
# Ensure log directory exists
STATUS_INTERVAL = 300  # Send Pi status every 5 min


# ------------------ Logging ------------------ #
def log_error(message):
    """
    Append a timestamped message to the error log file.

    Args:
        message (str): The message to log.
    """
    with open(ERROR_LOG, "a") as f:
        timestamp = datetime.utcnow().isoformat()
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
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except:
        return False


# ------------------ Fan Control ------------------ #
def setup_fan():
    """
    Initialize the GPIO and PWM for fan control.

    Returns:
        tuple: PWM object and initial duty cycle.
    """
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(FAN_PIN, GPIO.OUT)
    pwm = GPIO.PWM(FAN_PIN, PWM_FREQ)
    pwm.start(0)  # Start fully off
    log_error(f"[Fan] Initialized at 0% duty cycle")
    return pwm, 0

def update_fan(pwm, temp, current_duty):
    """
    Adjust fan speed for quiet operation targeting 40°C.
    Fan is completely off below OFF_TEMP.
    """
    if temp <= OFF_TEMP:
        new_duty = 0  # Fan fully off
    elif temp >= ON_TEMP:
        new_duty = 100  # Full speed
    else:
        # Smooth ramp from FAN_MIN_DUTY to 100% between OFF_TEMP and ON_TEMP
        slope = (100 - FAN_MIN_DUTY) / (ON_TEMP - OFF_TEMP)
        new_duty = int(FAN_MIN_DUTY + slope * (temp - OFF_TEMP))

    if new_duty != current_duty:
        pwm.ChangeDutyCycle(new_duty)

        if new_duty == 0:
            GPIO.output(FAN_PIN, GPIO.LOW)  # Ensure pin is LOW at 0%

        log_error(f"[Fan] Adjusted to {new_duty}% (CPU Temp: {temp}°C)")
    else:
        log_error(f"[Fan] Holding {current_duty}% (CPU Temp: {temp}°C)")

    return new_duty


# ------------------ Serial Read ------------------ #
def read_frame(ser):
    """
    Read a frame of VE.Direct data from the serial port.

    Args:
        ser (Serial): The open serial connection.

    Returns:
        dict: Parsed key-value pairs from the frame.
    """
    frame = {}
    while True:
        line = ser.readline().decode('utf-8', errors='ignore').strip()
        if not line:
            continue
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
        float: CPU temperature in °C, or 0.0 if unavailable.
    """
    try:
        result = subprocess.check_output(["vcgencmd", "measure_temp"]).decode()
        temp_str = result.strip().split("=")[1].split("'")[0]
        return float(temp_str)
    except:
        return 0.0


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
        resp = requests.post(POST_URL + "/bulk", json=entries, headers=headers, timeout=10)
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
    except:
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
    """
    Remove sent log entries from the log file to save space.
    """
    try:
        offset = get_last_sent_offset()
        if offset <= 0:
            return
        with open(LOG_PATH, "r+") as f:
            f.seek(offset)
            remaining_data = f.read()
            f.seek(0)
            f.write(remaining_data)
            f.truncate()
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
        with open(LOG_PATH, "r") as f:
            f.seek(offset)
            for line in f:
                try:
                    entries.append(json.loads(line))
                except:
                    continue
            if entries and bulk_upload(entries):
                update_last_sent_offset(f.tell())
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


def send_pi_status(current_duty):
    """
    Send the Raspberry Pi's system status and fan speed to the remote server.

    Args:
        current_duty (int): Current fan duty cycle (%).
    """
    if not network_ready():
        log_error("[Network] Offline: Skipping Pi status POST")
        return

    def safe(cmd, parse):
        try:
            return parse(subprocess.check_output(cmd).decode())
        except:
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
    memory = safe(["free", "-m"], lambda x: (
        lambda row: f"{row[2]}/{row[1]} ({int(int(''.join(filter(str.isdigit, row[2])))/int(''.join(filter(str.isdigit, row[1])))*100)}%)"
    )(x.splitlines()[1].split()))
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
        "fan_speed": f"{current_duty}%",
        "pi_name": hostname,
        "pi_os": os_version,
        "pi_updates": updates
    }

    try:
        # Send the status POST request
        headers = {"Authorization": f"Bearer {POST_SECRET}"}
        # Replace the log URL with the status URL
        status_url = POST_URL.replace("/log", "/status")
        # POST the status data
        r = requests.post(status_url, json=payload, headers=headers, timeout=5)
        # Check the response status
        if r.status_code == 200:
            log_error(f"[Status] POST succeeded: {r.status_code}")
        else:
            log_error(f"[Status] POST failed: {r.status_code} {r.text}")
    except Exception as e:
        log_error(f"[Status] Exception: {e}")

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

    try:
        ser = None
        while ser is None:
            try:
                ser = serial.Serial('/dev/ttyUSB0', 19200, timeout=5)
                log_error("[Serial] Connected to /dev/ttyUSB0")
            except Exception as e:
                log_error(f"[Serial] Open failed: {e}")
                time.sleep(30)

        upload_unsent_logs()

        last_prune_check = time.time()
        last_status_time = 0

        while True:
            try:
                frame = read_frame(ser)
                if frame:
                    frame["timestamp"] = datetime.utcnow().isoformat()
                    try:
                        with open(LOG_PATH, "a") as f:
                            json.dump(frame, f)
                            f.write("\n")
                        log_error(f"[Log] Frame written locally at {frame['timestamp']}")
                    except Exception as e:
                        log_error(f"[Log] Local write failed: {e}")

                    if network_ready():
                        try:
                            headers = {"Authorization": f"Bearer {POST_SECRET}"}
                            response = requests.post(POST_URL, json=frame, headers=headers, timeout=5)
                            if response.status_code == 200:
                                log_error(f"[Upload] POST succeeded: {response.status_code}")
                            else:
                                log_error(f"[Upload] POST failed: {response.status_code} {response.text}")
                        except Exception as e:
                            log_error(f"[Upload] Exception: {e}")
                    else:
                        log_error("[Network] Offline: Skipping frame upload")

                if time.time() - last_prune_check > 300:
                    upload_unsent_logs()
                    last_prune_check = time.time()

                if time.time() - last_status_time > STATUS_INTERVAL:
                    send_pi_status(current_duty)
                    last_status_time = time.time()

                temp = get_pi_temp()
                current_duty = update_fan(pwm, temp, current_duty)

            except Exception as e:
                log_error(f"[Loop] Error: {e}")

            time.sleep(60)

    finally:
        pwm.stop()
        GPIO.cleanup()
        log_error("[System] PWM stopped and GPIO cleaned up")


if __name__ == "__main__":
    """
    Entry point of the script.
    """
    main()
